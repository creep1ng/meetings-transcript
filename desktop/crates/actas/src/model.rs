//! UI-independent data: library snapshots from SQLite and the daemon status
//! stream.

use std::collections::HashMap;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::Duration;

use actas_core::Db;
use actas_core::db::{Mark, Meeting, Segment, Speaker};
use actas_core::ipc::{self, DaemonStatus, Event};
use anyhow::Result;
use chrono::{Datelike, Local};

/// Everything shown for one meeting.
#[derive(Clone, Default)]
pub struct Detail {
    pub meeting: Option<Meeting>,
    pub speakers: Vec<Speaker>,
    pub segments: Vec<Segment>,
    pub marks: Vec<Mark>,
}

impl Detail {
    pub fn load(db: &Db, id: &str) -> Result<Self> {
        Ok(Self {
            meeting: db.meeting(id)?,
            speakers: db.speakers(id)?,
            segments: db.segments(id)?,
            marks: db.marks(id)?,
        })
    }

    pub fn speaker(&self, id: Option<i64>) -> Option<&Speaker> {
        id.and_then(|id| self.speakers.iter().find(|s| s.id == id))
    }

    /// Speaker turns for the timeline: `(speaker color index, start, end)`.
    pub fn turns(&self) -> Vec<(i64, i64, i64)> {
        let colors: HashMap<i64, i64> = self.speakers.iter().map(|s| (s.id, s.color)).collect();
        self.segments
            .iter()
            .filter_map(|s| s.speaker_id.and_then(|id| colors.get(&id)).map(|c| (*c, s.start_ms, s.end_ms)))
            .collect()
    }

    /// Index of the segment playing at `ms`.
    pub fn segment_at(&self, ms: i64) -> Option<usize> {
        self.segments.iter().rposition(|s| s.start_ms <= ms && ms < s.end_ms + 800)
    }
}

/// Sidebar group for a meeting date.
pub fn day_group(m: &Meeting) -> &'static str {
    let today = Local::now().date_naive();
    let d = m.started_local().date_naive();
    let days = (today - d).num_days();
    match days {
        0 => "Hoy",
        1 => "Ayer",
        _ if days < 7 && d.iso_week() == today.iso_week() => "Esta semana",
        _ if d.month() == today.month() && d.year() == today.year() => "Este mes",
        _ => "Anteriores",
    }
}

#[allow(clippy::large_enum_variant)]
pub enum DaemonMsg {
    Status(DaemonStatus),
    /// A meeting changed; the id is not needed, the window reloads the list.
    Library(#[allow(dead_code)] String),
    Offline,
}

/// Follows the daemon from a background thread, reconnecting every 2 s.
pub fn follow_daemon() -> Receiver<DaemonMsg> {
    let (tx, rx) = channel::<DaemonMsg>();
    std::thread::Builder::new().name("actas-ipc".into()).spawn(move || follow(tx)).expect("hilo IPC");
    rx
}

fn follow(tx: Sender<DaemonMsg>) {
    loop {
        let mut alive = true;
        let res = ipc::subscribe(|ev| {
            let msg = match ev {
                Event::Status(s) => DaemonMsg::Status(s),
                Event::LibraryChanged { meeting_id } => DaemonMsg::Library(meeting_id),
            };
            alive = tx.send(msg).is_ok();
            alive
        });
        if !alive || tx.send(DaemonMsg::Offline).is_err() {
            return;
        }
        if let Err(e) = res {
            tracing::debug!("sin demonio: {e:#}");
        }
        std::thread::sleep(Duration::from_secs(2));
    }
}

/// Sends a request on a background thread; the result goes to `done`.
pub fn request_async(req: ipc::Request, done: impl FnOnce(Result<ipc::Response>) + Send + 'static) {
    std::thread::spawn(move || done(ipc::call(&req)));
}

pub fn human_bytes(b: u64) -> String {
    let b = b as f64;
    if b >= 1e9 { format!("{:.1} GB", b / 1e9) } else { format!("{:.0} MB", b / 1e6) }
}
