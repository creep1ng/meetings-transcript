//! SQLite library shared by the daemon (writer of recordings and transcripts)
//! and the GUI (reads, renames, voice management). WAL mode lets both open it.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use chrono::{DateTime, Local, TimeZone};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};

use crate::voice;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS meetings (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    platform    TEXT,
    app         TEXT,
    started_at  INTEGER NOT NULL,          -- unix ms
    duration_ms INTEGER NOT NULL DEFAULT 0,
    dir         TEXT NOT NULL,
    has_video   INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL,             -- recording | queued | transcribing | done | failed
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT,
    error       TEXT,
    model       TEXT
);
CREATE TABLE IF NOT EXISTS marks (
    id         INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    at_ms      INTEGER NOT NULL,
    label      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voices (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    embedding  BLOB NOT NULL,
    samples    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS speakers (
    id         INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    track      TEXT NOT NULL,              -- mic | system
    label      TEXT NOT NULL,              -- "Yo", "Hablante 1"...
    name       TEXT,                       -- user rename or matched voice
    voice_id   INTEGER REFERENCES voices(id) ON DELETE SET NULL,
    similarity REAL,
    color      INTEGER NOT NULL,
    embedding  BLOB
);
CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_meeting ON segments(meeting_id, start_ms);
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, content='segments', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"#;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Recording,
    Queued,
    Transcribing,
    Done,
    Failed,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Recording => "recording",
            Self::Queued => "queued",
            Self::Transcribing => "transcribing",
            Self::Done => "done",
            Self::Failed => "failed",
        }
    }

    fn parse(s: &str) -> Self {
        match s {
            "recording" => Self::Recording,
            "queued" => Self::Queued,
            "transcribing" => Self::Transcribing,
            "done" => Self::Done,
            _ => Self::Failed,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Meeting {
    pub id: String,
    pub title: String,
    pub platform: Option<String>,
    pub app: Option<String>,
    pub started_at: i64,
    pub duration_ms: i64,
    pub dir: PathBuf,
    pub has_video: bool,
    pub status: Status,
    pub progress: f32,
    pub stage: Option<String>,
    pub error: Option<String>,
    pub model: Option<String>,
}

impl Meeting {
    pub fn started_local(&self) -> DateTime<Local> {
        Local.timestamp_millis_opt(self.started_at).single().unwrap_or_else(Local::now)
    }

    pub fn video_path(&self) -> PathBuf {
        self.dir.join("recording.mkv")
    }

    pub fn mic_wav(&self) -> PathBuf {
        self.dir.join("mic.wav")
    }

    pub fn system_wav(&self) -> PathBuf {
        self.dir.join("system.wav")
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Speaker {
    pub id: i64,
    pub track: String,
    pub label: String,
    pub name: Option<String>,
    pub voice_id: Option<i64>,
    pub similarity: Option<f32>,
    pub color: i64,
    pub has_embedding: bool,
}

impl Speaker {
    pub fn display_name(&self) -> &str {
        self.name.as_deref().unwrap_or(&self.label)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Segment {
    pub id: i64,
    pub speaker_id: Option<i64>,
    pub start_ms: i64,
    pub end_ms: i64,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Mark {
    pub id: i64,
    pub at_ms: i64,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Voice {
    pub id: i64,
    pub name: String,
    pub samples: i64,
    pub meetings: i64,
    pub updated_at: i64,
}

/// A transcript produced by the engine, ready to be stored.
#[derive(Debug, Clone, Default)]
pub struct NewTranscript {
    pub model: String,
    pub speakers: Vec<NewSpeaker>,
    /// `speaker` indexes into `speakers`.
    pub segments: Vec<NewSegment>,
}

#[derive(Debug, Clone)]
pub struct NewSpeaker {
    pub track: String,
    pub label: String,
    pub name: Option<String>,
    pub voice_id: Option<i64>,
    pub similarity: Option<f32>,
    pub embedding: Option<Vec<f32>>,
}

#[derive(Debug, Clone)]
pub struct NewSegment {
    pub speaker: Option<usize>,
    pub start_ms: i64,
    pub end_ms: i64,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SearchHit {
    pub meeting_id: String,
    pub segment_id: i64,
    pub start_ms: i64,
    pub snippet: String,
}

pub struct Db {
    conn: Connection,
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

impl Db {
    pub fn open_default() -> Result<Self> {
        Self::open(&crate::paths::db_file())
    }

    pub fn open(path: &Path) -> Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let conn = Connection::open(path).with_context(|| format!("abriendo {}", path.display()))?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self { conn })
    }

    // ---------- meetings ----------

    pub fn create_meeting(&self, m: &Meeting) -> Result<()> {
        self.conn.execute(
            "INSERT INTO meetings (id, title, platform, app, started_at, duration_ms, dir, has_video, status, progress)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 0)",
            params![
                m.id,
                m.title,
                m.platform,
                m.app,
                m.started_at,
                m.duration_ms,
                m.dir.to_string_lossy(),
                m.has_video,
                m.status.as_str()
            ],
        )?;
        Ok(())
    }

    fn row_meeting(r: &rusqlite::Row) -> rusqlite::Result<Meeting> {
        Ok(Meeting {
            id: r.get(0)?,
            title: r.get(1)?,
            platform: r.get(2)?,
            app: r.get(3)?,
            started_at: r.get(4)?,
            duration_ms: r.get(5)?,
            dir: PathBuf::from(r.get::<_, String>(6)?),
            has_video: r.get(7)?,
            status: Status::parse(&r.get::<_, String>(8)?),
            progress: r.get::<_, f64>(9)? as f32,
            stage: r.get(10)?,
            error: r.get(11)?,
            model: r.get(12)?,
        })
    }

    const MEETING_COLS: &str =
        "id, title, platform, app, started_at, duration_ms, dir, has_video, status, progress, stage, error, model";

    pub fn meetings(&self) -> Result<Vec<Meeting>> {
        let mut st =
            self.conn.prepare(&format!("SELECT {} FROM meetings ORDER BY started_at DESC", Self::MEETING_COLS))?;
        let rows = st.query_map([], Self::row_meeting)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn meeting(&self, id: &str) -> Result<Option<Meeting>> {
        Ok(self
            .conn
            .query_row(&format!("SELECT {} FROM meetings WHERE id = ?1", Self::MEETING_COLS), [id], Self::row_meeting)
            .optional()?)
    }

    pub fn rename_meeting(&self, id: &str, title: &str) -> Result<()> {
        self.conn.execute("UPDATE meetings SET title = ?2 WHERE id = ?1", params![id, title])?;
        Ok(())
    }

    pub fn finish_recording(&self, id: &str, duration_ms: i64, next: Status) -> Result<()> {
        self.conn.execute(
            "UPDATE meetings SET duration_ms = ?2, status = ?3, progress = 0 WHERE id = ?1",
            params![id, duration_ms, next.as_str()],
        )?;
        Ok(())
    }

    pub fn set_status(&self, id: &str, status: Status, error: Option<&str>) -> Result<()> {
        self.conn.execute(
            "UPDATE meetings SET status = ?2, error = ?3, stage = NULL WHERE id = ?1",
            params![id, status.as_str(), error],
        )?;
        Ok(())
    }

    pub fn set_progress(&self, id: &str, progress: f32, stage: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE meetings SET progress = ?2, stage = ?3 WHERE id = ?1",
            params![id, progress as f64, stage],
        )?;
        Ok(())
    }

    /// Oldest queued meeting, i.e. the next transcription job.
    pub fn next_queued(&self) -> Result<Option<Meeting>> {
        Ok(self
            .conn
            .query_row(
                &format!(
                    "SELECT {} FROM meetings WHERE status = 'queued' ORDER BY started_at LIMIT 1",
                    Self::MEETING_COLS
                ),
                [],
                Self::row_meeting,
            )
            .optional()?)
    }

    /// After a crash: interrupted transcriptions go back to the queue and
    /// recordings without a running recorder are closed.
    pub fn recover_interrupted(&self) -> Result<()> {
        self.conn.execute(
            "UPDATE meetings SET status = 'queued', progress = 0, stage = NULL WHERE status = 'transcribing'",
            [],
        )?;
        self.conn.execute("UPDATE meetings SET status = 'queued' WHERE status = 'recording'", [])?;
        Ok(())
    }

    pub fn delete_meeting(&self, id: &str) -> Result<Option<PathBuf>> {
        let dir = self.meeting(id)?.map(|m| m.dir);
        self.conn.execute("DELETE FROM meetings WHERE id = ?1", [id])?;
        Ok(dir)
    }

    // ---------- marks ----------

    pub fn add_mark(&self, meeting_id: &str, at_ms: i64, label: &str) -> Result<()> {
        self.conn.execute(
            "INSERT INTO marks (meeting_id, at_ms, label) VALUES (?1, ?2, ?3)",
            params![meeting_id, at_ms, label],
        )?;
        Ok(())
    }

    pub fn marks(&self, meeting_id: &str) -> Result<Vec<Mark>> {
        let mut st = self.conn.prepare("SELECT id, at_ms, label FROM marks WHERE meeting_id = ?1 ORDER BY at_ms")?;
        let rows = st.query_map([meeting_id], |r| Ok(Mark { id: r.get(0)?, at_ms: r.get(1)?, label: r.get(2)? }))?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    // ---------- transcript ----------

    /// Replaces the transcript of a meeting and marks it done.
    pub fn store_transcript(&mut self, meeting_id: &str, t: &NewTranscript) -> Result<()> {
        let tx = self.conn.transaction()?;
        tx.execute("DELETE FROM segments WHERE meeting_id = ?1", [meeting_id])?;
        tx.execute("DELETE FROM speakers WHERE meeting_id = ?1", [meeting_id])?;
        let mut ids = Vec::with_capacity(t.speakers.len());
        for (i, s) in t.speakers.iter().enumerate() {
            tx.execute(
                "INSERT INTO speakers (meeting_id, track, label, name, voice_id, similarity, color, embedding)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![
                    meeting_id,
                    s.track,
                    s.label,
                    s.name,
                    s.voice_id,
                    s.similarity.map(f64::from),
                    i as i64,
                    s.embedding.as_deref().map(voice::to_blob)
                ],
            )?;
            ids.push(tx.last_insert_rowid());
        }
        {
            let mut st = tx.prepare(
                "INSERT INTO segments (meeting_id, speaker_id, start_ms, end_ms, text) VALUES (?1, ?2, ?3, ?4, ?5)",
            )?;
            for seg in &t.segments {
                let speaker = seg.speaker.and_then(|i| ids.get(i).copied());
                st.execute(params![meeting_id, speaker, seg.start_ms, seg.end_ms, seg.text])?;
            }
        }
        tx.execute(
            "UPDATE meetings SET status = 'done', progress = 1, stage = NULL, error = NULL, model = ?2 WHERE id = ?1",
            params![meeting_id, t.model],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub fn speakers(&self, meeting_id: &str) -> Result<Vec<Speaker>> {
        let mut st = self.conn.prepare(
            "SELECT id, track, label, name, voice_id, similarity, color, embedding IS NOT NULL
             FROM speakers WHERE meeting_id = ?1 ORDER BY color",
        )?;
        let rows = st.query_map([meeting_id], |r| {
            Ok(Speaker {
                id: r.get(0)?,
                track: r.get(1)?,
                label: r.get(2)?,
                name: r.get(3)?,
                voice_id: r.get(4)?,
                similarity: r.get::<_, Option<f64>>(5)?.map(|v| v as f32),
                color: r.get(6)?,
                has_embedding: r.get(7)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn segments(&self, meeting_id: &str) -> Result<Vec<Segment>> {
        let mut st = self.conn.prepare(
            "SELECT id, speaker_id, start_ms, end_ms, text FROM segments WHERE meeting_id = ?1 ORDER BY start_ms, id",
        )?;
        let rows = st.query_map([meeting_id], |r| {
            Ok(Segment {
                id: r.get(0)?,
                speaker_id: r.get(1)?,
                start_ms: r.get(2)?,
                end_ms: r.get(3)?,
                text: r.get(4)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn rename_speaker(&self, speaker_id: i64, name: Option<&str>) -> Result<()> {
        let name = name.map(str::trim).filter(|n| !n.is_empty());
        self.conn.execute(
            "UPDATE speakers SET name = ?2, voice_id = NULL, similarity = NULL WHERE id = ?1",
            params![speaker_id, name],
        )?;
        Ok(())
    }

    /// Full-text search over every transcript.
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<SearchHit>> {
        let terms: Vec<String> = query
            .split_whitespace()
            .map(|w| w.chars().filter(|c| c.is_alphanumeric()).collect::<String>())
            .filter(|w| !w.is_empty())
            .map(|w| format!("\"{w}\"*"))
            .collect();
        if terms.is_empty() {
            return Ok(Vec::new());
        }
        let mut st = self.conn.prepare(
            "SELECT s.meeting_id, s.id, s.start_ms, snippet(segments_fts, 0, '«', '»', '…', 12)
             FROM segments_fts JOIN segments s ON s.id = segments_fts.rowid
             WHERE segments_fts MATCH ?1 ORDER BY rank LIMIT ?2",
        )?;
        let rows = st.query_map(params![terms.join(" "), limit as i64], |r| {
            Ok(SearchHit { meeting_id: r.get(0)?, segment_id: r.get(1)?, start_ms: r.get(2)?, snippet: r.get(3)? })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    // ---------- voices ----------

    pub fn voices(&self) -> Result<Vec<Voice>> {
        let mut st = self.conn.prepare(
            "SELECT v.id, v.name, v.samples, v.updated_at,
                    (SELECT COUNT(DISTINCT meeting_id) FROM speakers s WHERE s.voice_id = v.id)
             FROM voices v ORDER BY v.name COLLATE NOCASE",
        )?;
        let rows = st.query_map([], |r| {
            Ok(Voice { id: r.get(0)?, name: r.get(1)?, samples: r.get(2)?, updated_at: r.get(3)?, meetings: r.get(4)? })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// All voice embeddings, for matching.
    pub fn voice_embeddings(&self) -> Result<Vec<(i64, String, Vec<f32>)>> {
        let mut st = self.conn.prepare("SELECT id, name, embedding FROM voices")?;
        let rows = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, voice::from_blob(&r.get::<_, Vec<u8>>(2)?))))?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// Saves the voice of a meeting speaker under `name`. If a voice with that
    /// name exists, the new sample is averaged into it.
    pub fn save_voice_from_speaker(&mut self, speaker_id: i64, name: &str) -> Result<i64> {
        let name = name.trim();
        if name.is_empty() {
            bail!("el nombre de la voz está vacío");
        }
        let tx = self.conn.transaction()?;
        let emb: Option<Vec<u8>> = tx
            .query_row("SELECT embedding FROM speakers WHERE id = ?1", [speaker_id], |r| r.get(0))
            .optional()?
            .flatten();
        let Some(emb) = emb else { bail!("este hablante no tiene huella de voz (habló muy poco)") };
        let emb = voice::from_blob(&emb);
        let existing: Option<(i64, Vec<u8>, i64)> = tx
            .query_row("SELECT id, embedding, samples FROM voices WHERE name = ?1", [name], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })
            .optional()?;
        let now = now_ms();
        let voice_id = match existing {
            Some((id, old, n)) => {
                let merged = voice::merge(&voice::from_blob(&old), n, &emb);
                tx.execute(
                    "UPDATE voices SET embedding = ?2, samples = samples + 1, updated_at = ?3 WHERE id = ?1",
                    params![id, voice::to_blob(&merged), now],
                )?;
                id
            }
            None => {
                tx.execute(
                    "INSERT INTO voices (name, embedding, samples, created_at, updated_at) VALUES (?1, ?2, 1, ?3, ?3)",
                    params![name, voice::to_blob(&voice::normalized(&emb)), now],
                )?;
                tx.last_insert_rowid()
            }
        };
        tx.execute(
            "UPDATE speakers SET name = ?2, voice_id = ?3, similarity = 1.0 WHERE id = ?1",
            params![speaker_id, name, voice_id],
        )?;
        tx.commit()?;
        Ok(voice_id)
    }

    pub fn rename_voice(&self, voice_id: i64, name: &str) -> Result<()> {
        self.conn.execute("UPDATE voices SET name = ?2 WHERE id = ?1", params![voice_id, name.trim()])?;
        self.conn.execute("UPDATE speakers SET name = ?2 WHERE voice_id = ?1", params![voice_id, name.trim()])?;
        Ok(())
    }

    /// Deletes a voice; speakers named after it go back to their generic label.
    pub fn delete_voice(&self, voice_id: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE speakers SET name = NULL, similarity = NULL, voice_id = NULL WHERE voice_id = ?1",
            [voice_id],
        )?;
        self.conn.execute("DELETE FROM voices WHERE id = ?1", [voice_id])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn meeting(id: &str, started_at: i64) -> Meeting {
        Meeting {
            id: id.into(),
            title: format!("Reunión {id}"),
            platform: Some("Meet".into()),
            app: Some("brave".into()),
            started_at,
            duration_ms: 0,
            dir: PathBuf::from("/tmp").join(id),
            has_video: true,
            status: Status::Recording,
            progress: 0.0,
            stage: None,
            error: None,
            model: None,
        }
    }

    fn transcript() -> NewTranscript {
        NewTranscript {
            model: "test".into(),
            speakers: vec![
                NewSpeaker {
                    track: "mic".into(),
                    label: "Yo".into(),
                    name: None,
                    voice_id: None,
                    similarity: None,
                    embedding: None,
                },
                NewSpeaker {
                    track: "system".into(),
                    label: "Hablante 1".into(),
                    name: None,
                    voice_id: None,
                    similarity: None,
                    embedding: Some(vec![1.0, 0.0, 0.0]),
                },
            ],
            segments: vec![
                NewSegment {
                    speaker: Some(1),
                    start_ms: 1000,
                    end_ms: 4000,
                    text: "El despliegue pasa al jueves".into(),
                },
                NewSegment { speaker: Some(0), start_ms: 5000, end_ms: 7000, text: "Necesito staging antes".into() },
            ],
        }
    }

    #[test]
    fn transcript_lifecycle_and_search() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = Db::open(&dir.path().join("a.db")).unwrap();
        db.create_meeting(&meeting("m1", 10)).unwrap();
        db.create_meeting(&meeting("m2", 20)).unwrap();
        db.finish_recording("m1", 60_000, Status::Queued).unwrap();
        assert_eq!(db.next_queued().unwrap().unwrap().id, "m1");
        db.store_transcript("m1", &transcript()).unwrap();
        // storing twice replaces instead of duplicating
        db.store_transcript("m1", &transcript()).unwrap();
        assert_eq!(db.segments("m1").unwrap().len(), 2);
        let m = db.meeting("m1").unwrap().unwrap();
        assert_eq!(m.status, Status::Done);
        assert_eq!(m.duration_ms, 60_000);
        let hits = db.search("despliegue jue", 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].meeting_id, "m1");
        assert!(hits[0].snippet.contains("«despliegue»"));
        assert_eq!(db.meetings().unwrap()[0].id, "m2");
    }

    #[test]
    fn voices_save_merge_delete() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = Db::open(&dir.path().join("a.db")).unwrap();
        db.create_meeting(&meeting("m1", 10)).unwrap();
        db.store_transcript("m1", &transcript()).unwrap();
        let sp = db.speakers("m1").unwrap();
        let (me, other) = (&sp[0], &sp[1]);
        assert!(db.save_voice_from_speaker(me.id, "Yo").is_err(), "sin embedding");
        let v = db.save_voice_from_speaker(other.id, "Laura").unwrap();
        assert_eq!(db.save_voice_from_speaker(other.id, "Laura").unwrap(), v);
        let voices = db.voices().unwrap();
        assert_eq!(voices.len(), 1);
        assert_eq!((voices[0].samples, voices[0].meetings), (2, 1));
        assert_eq!(db.speakers("m1").unwrap()[1].display_name(), "Laura");
        db.delete_voice(v).unwrap();
        assert!(db.voices().unwrap().is_empty());
        assert_eq!(db.speakers("m1").unwrap()[1].display_name(), "Hablante 1");
    }

    #[test]
    fn recovers_interrupted_jobs() {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(&dir.path().join("a.db")).unwrap();
        db.create_meeting(&meeting("m1", 10)).unwrap();
        db.set_status("m1", Status::Transcribing, None).unwrap();
        db.recover_interrupted().unwrap();
        assert_eq!(db.meeting("m1").unwrap().unwrap().status, Status::Queued);
    }
}
