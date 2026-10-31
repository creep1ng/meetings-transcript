//! Daemon protocol: newline-delimited JSON over the Unix socket
//! [`crate::paths::socket_path`]. One request per line, one response per line.
//! After `Subscribe`, the daemon keeps the connection and pushes `Event`s.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

use crate::paths;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    Status,
    /// Start recording. `title: None` uses the detected window title.
    Start {
        title: Option<String>,
        video: bool,
    },
    Stop,
    /// Start with video if idle, stop otherwise.
    Toggle,
    TogglePause,
    Mark {
        label: Option<String>,
    },
    /// Answer to the "Llamada detectada" suggestion.
    AcceptSuggestion {
        video: bool,
    },
    DismissSuggestion,
    /// Queue (or re-queue) a meeting for transcription.
    Transcribe {
        meeting_id: String,
    },
    ReloadConfig,
    Subscribe,
    Quit,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)]
pub enum Response {
    Ok,
    Status(DaemonStatus),
    Error { message: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)]
pub enum Event {
    Status(DaemonStatus),
    /// A meeting row changed (new recording, progress, transcript stored).
    LibraryChanged {
        meeting_id: String,
    },
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DaemonStatus {
    pub recording: Option<RecordingStatus>,
    pub suggestion: Option<Suggestion>,
    pub transcribing: Option<JobStatus>,
    pub queued: usize,
    /// Missing model files; transcription waits until they exist.
    pub missing_models: Vec<String>,
}

impl DaemonStatus {
    /// `idle` | `suggest` | `recording` | `paused`, for bar widgets.
    pub fn state_name(&self) -> &'static str {
        match &self.recording {
            Some(r) if r.paused => "paused",
            Some(_) => "recording",
            None if self.suggestion.is_some() => "suggest",
            None => "idle",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RecordingStatus {
    pub meeting_id: String,
    pub title: String,
    pub platform: Option<String>,
    pub elapsed_ms: i64,
    pub paused: bool,
    pub video: bool,
    /// What the portal is capturing, e.g. "Ventana «Meet – Brave»".
    pub source: Option<String>,
    pub encoder: Option<String>,
    /// Peak levels, 0..1.
    pub mic_level: f32,
    pub system_level: f32,
    pub bytes_written: u64,
    pub disk_free: u64,
    /// The call that triggered the recording seems to have ended.
    pub call_ended: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Suggestion {
    pub app: String,
    pub title: Option<String>,
    pub platform: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JobStatus {
    pub meeting_id: String,
    pub title: String,
    pub progress: f32,
    pub stage: String,
}

/// Blocking client for one request/response round trip.
pub fn request(req: &Request) -> Result<Response> {
    let stream = connect()?;
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    send(&stream, req)?;
    let mut line = String::new();
    BufReader::new(&stream).read_line(&mut line)?;
    if line.is_empty() {
        bail!("el demonio cerró la conexión");
    }
    Ok(serde_json::from_str(&line)?)
}

/// Like [`request`] but turns `Response::Error` into an `Err`.
pub fn call(req: &Request) -> Result<Response> {
    match request(req)? {
        Response::Error { message } => bail!(message),
        r => Ok(r),
    }
}

pub fn connect() -> Result<UnixStream> {
    let path = paths::socket_path();
    UnixStream::connect(&path)
        .with_context(|| format!("no se pudo conectar con actasd en {} (¿está en marcha?)", path.display()))
}

fn send(mut stream: &UnixStream, req: &Request) -> Result<()> {
    let mut line = serde_json::to_vec(req)?;
    line.push(b'\n');
    stream.write_all(&line)?;
    Ok(())
}

/// Subscribes and calls `on_event` for every event until the connection drops
/// or `on_event` returns `false`.
pub fn subscribe(mut on_event: impl FnMut(Event) -> bool) -> Result<()> {
    let stream = connect()?;
    send(&stream, &Request::Subscribe)?;
    let reader = BufReader::new(&stream);
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        // The first line is the `Ok` response to Subscribe.
        if let Ok(ev) = serde_json::from_str::<Event>(&line)
            && !on_event(ev)
        {
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_format_is_stable() {
        let j = serde_json::to_string(&Request::Start { title: None, video: true }).unwrap();
        assert_eq!(j, r#"{"cmd":"start","title":null,"video":true}"#);
        let r: Request = serde_json::from_str(r#"{"cmd":"mark","label":"tarea"}"#).unwrap();
        assert_eq!(r, Request::Mark { label: Some("tarea".into()) });
        let e = serde_json::to_string(&Event::LibraryChanged { meeting_id: "x".into() }).unwrap();
        assert_eq!(e, r#"{"event":"library_changed","meeting_id":"x"}"#);
    }
}
