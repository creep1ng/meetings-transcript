//! Transcript exports: Markdown, plain text, SRT and JSON.

use std::collections::HashMap;

use serde::Serialize;

use crate::db::{Mark, Meeting, Segment, Speaker};
use crate::fmt_ms;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Markdown,
    Text,
    Srt,
    Json,
}

impl Format {
    pub const ALL: [Self; 4] = [Self::Markdown, Self::Text, Self::Srt, Self::Json];

    pub fn extension(self) -> &'static str {
        match self {
            Self::Markdown => "md",
            Self::Text => "txt",
            Self::Srt => "srt",
            Self::Json => "json",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Markdown => "Markdown",
            Self::Text => "Texto",
            Self::Srt => "Subtítulos SRT",
            Self::Json => "JSON",
        }
    }
}

pub struct Transcript<'a> {
    pub meeting: &'a Meeting,
    pub speakers: &'a [Speaker],
    pub segments: &'a [Segment],
    pub marks: &'a [Mark],
}

impl Transcript<'_> {
    fn names(&self) -> HashMap<i64, &str> {
        self.speakers.iter().map(|s| (s.id, s.display_name())).collect()
    }

    pub fn render(&self, format: Format) -> String {
        match format {
            Format::Markdown => self.markdown(),
            Format::Text => self.text(),
            Format::Srt => self.srt(),
            Format::Json => self.json(),
        }
    }

    pub fn markdown(&self) -> String {
        let names = self.names();
        let m = self.meeting;
        let mut out = format!("# {}\n\n", m.title);
        out += &format!("- Fecha: {}\n", m.started_local().format("%Y-%m-%d %H:%M"));
        out += &format!("- Duración: {}\n", fmt_ms(m.duration_ms));
        if let Some(p) = &m.platform {
            out += &format!("- Plataforma: {p}\n");
        }
        let people: Vec<&str> = self.speakers.iter().map(Speaker::display_name).collect();
        if !people.is_empty() {
            out += &format!("- Participantes: {}\n", people.join(", "));
        }
        if !self.marks.is_empty() {
            out += "\n## Marcas\n\n";
            for mk in self.marks {
                out += &format!("- `{}` {}\n", fmt_ms(mk.at_ms), mk.label);
            }
        }
        out += "\n## Transcripción\n\n";
        let mut last: Option<Option<i64>> = None;
        for seg in self.segments {
            if last != Some(seg.speaker_id) {
                let who = seg.speaker_id.and_then(|id| names.get(&id).copied()).unwrap_or("?");
                out += &format!("\n**{who}** `{}`\n\n", fmt_ms(seg.start_ms));
                last = Some(seg.speaker_id);
            }
            out += &format!("{}\n", seg.text.trim());
        }
        out
    }

    pub fn text(&self) -> String {
        let names = self.names();
        let mut out = String::new();
        for seg in self.segments {
            let who = seg.speaker_id.and_then(|id| names.get(&id).copied()).unwrap_or("?");
            out += &format!("[{}] {who}: {}\n", fmt_ms(seg.start_ms), seg.text.trim());
        }
        out
    }

    pub fn srt(&self) -> String {
        fn ts(ms: i64) -> String {
            let ms = ms.max(0);
            format!("{:02}:{:02}:{:02},{:03}", ms / 3_600_000, (ms / 60_000) % 60, (ms / 1000) % 60, ms % 1000)
        }
        let names = self.names();
        let mut out = String::new();
        for (i, seg) in self.segments.iter().enumerate() {
            let who = seg.speaker_id.and_then(|id| names.get(&id).copied()).unwrap_or("?");
            out += &format!("{}\n{} --> {}\n{who}: {}\n\n", i + 1, ts(seg.start_ms), ts(seg.end_ms), seg.text.trim());
        }
        out
    }

    pub fn json(&self) -> String {
        #[derive(Serialize)]
        struct Out<'a> {
            meeting: &'a Meeting,
            speakers: &'a [Speaker],
            segments: &'a [Segment],
            marks: &'a [Mark],
        }
        serde_json::to_string_pretty(&Out {
            meeting: self.meeting,
            speakers: self.speakers,
            segments: self.segments,
            marks: self.marks,
        })
        .unwrap_or_default()
    }
}

/// Writes `transcript.md` next to the recording (what Codex reads). Called
/// after transcribing and after renaming a meeting or a speaker.
pub fn write_transcript_md(db: &crate::Db, id: &str) -> anyhow::Result<()> {
    let Some(m) = db.meeting(id)? else { return Ok(()) };
    let (speakers, segments, marks) = (db.speakers(id)?, db.segments(id)?, db.marks(id)?);
    let t = Transcript { meeting: &m, speakers: &speakers, segments: &segments, marks: &marks };
    std::fs::write(m.dir.join("transcript.md"), t.render(Format::Markdown))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::db::Status;

    #[test]
    fn srt_and_markdown() {
        let meeting = Meeting {
            id: "m".into(),
            title: "Sync".into(),
            platform: None,
            app: None,
            started_at: 0,
            duration_ms: 65_000,
            dir: PathBuf::new(),
            has_video: false,
            status: Status::Done,
            progress: 1.0,
            stage: None,
            error: None,
            model: None,
        };
        let speakers = vec![Speaker {
            id: 7,
            track: "mic".into(),
            label: "Yo".into(),
            name: None,
            voice_id: None,
            similarity: None,
            color: 0,
            has_embedding: false,
        }];
        let segments = vec![
            Segment { id: 1, speaker_id: Some(7), start_ms: 1_500, end_ms: 3_250, text: "Hola".into() },
            Segment { id: 2, speaker_id: Some(7), start_ms: 4_000, end_ms: 5_000, text: "Qué tal".into() },
        ];
        let t = Transcript { meeting: &meeting, speakers: &speakers, segments: &segments, marks: &[] };
        assert!(t.srt().starts_with("1\n00:00:01,500 --> 00:00:03,250\nYo: Hola\n"));
        let md = t.markdown();
        assert_eq!(md.matches("**Yo**").count(), 1, "consecutive segments share a header");
        assert!(md.contains("- Duración: 01:05"));
    }
}
