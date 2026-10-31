//! Transcription queue. Runs on its own thread: picks the oldest `queued`
//! meeting, keeps the models loaded while there is work and frees the VRAM
//! after `unload_after_secs` idle.

use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use actas_core::db::{Meeting, Status};
use actas_core::ipc::JobStatus;
use actas_core::{Db, fmt_ms};
use actas_engine::{Engine, Job};
use anyhow::Result;

use crate::daemon::Daemon;

pub enum Msg {
    Wake,
    Quit,
}

pub fn run(daemon: Daemon, rx: Receiver<Msg>) {
    let db = match Db::open_default() {
        Ok(db) => db,
        Err(e) => {
            tracing::error!("el trabajador no pudo abrir la base de datos: {e:#}");
            return;
        }
    };
    let mut engine: Option<Engine> = None;
    let mut last_job = Instant::now();
    loop {
        // Drain the queue.
        loop {
            let cfg = daemon.config();
            if !actas_engine::missing_models(&cfg.transcription).is_empty() {
                daemon.set_job(None, queued(&db));
                break;
            }
            let Ok(Some(meeting)) = db.next_queued() else { break };
            if engine.is_none() {
                report(&daemon, &db, &meeting, 0.0, "Cargando modelos");
                match Engine::load(&cfg.transcription) {
                    Ok(e) => engine = Some(e),
                    Err(e) => {
                        tracing::error!("no pude cargar los modelos: {e:#}");
                        let _ = db.set_status(&meeting.id, Status::Failed, Some(&format!("{e:#}")));
                        daemon.emit_library(&meeting.id);
                        continue;
                    }
                }
            }
            let engine = engine.as_mut().unwrap();
            let started = Instant::now();
            match transcribe(&daemon, &db, engine, &meeting, cfg.voices.threshold) {
                Ok(()) => tracing::info!(
                    "«{}» transcrita en {:.0}s ({} de audio)",
                    meeting.title,
                    started.elapsed().as_secs_f32(),
                    fmt_ms(meeting.duration_ms)
                ),
                Err(e) => {
                    tracing::error!("falló la transcripción de «{}»: {e:#}", meeting.title);
                    let _ = db.set_status(&meeting.id, Status::Failed, Some(&format!("{e:#}")));
                }
            }
            daemon.emit_library(&meeting.id);
            last_job = Instant::now();
        }
        daemon.set_job(None, queued(&db));

        let unload_after = Duration::from_secs(daemon.config().transcription.unload_after_secs);
        match rx.recv_timeout(Duration::from_secs(15)) {
            Ok(Msg::Wake) => {}
            Ok(Msg::Quit) | Err(RecvTimeoutError::Disconnected) => return,
            Err(RecvTimeoutError::Timeout) => {
                if engine.is_some() && last_job.elapsed() >= unload_after {
                    tracing::info!("libero los modelos tras {}s sin trabajo", unload_after.as_secs());
                    engine = None;
                }
            }
        }
    }
}

fn queued(db: &Db) -> usize {
    db.meetings().map(|m| m.iter().filter(|m| m.status == Status::Queued).count()).unwrap_or(0)
}

fn report(daemon: &Daemon, db: &Db, m: &Meeting, progress: f32, stage: &str) {
    let _ = db.set_progress(&m.id, progress, stage);
    daemon.set_job(
        Some(JobStatus { meeting_id: m.id.clone(), title: m.title.clone(), progress, stage: stage.to_string() }),
        queued(db),
    );
}

fn transcribe(daemon: &Daemon, db: &Db, engine: &mut Engine, m: &Meeting, threshold: f32) -> Result<()> {
    db.set_status(&m.id, Status::Transcribing, None)?;
    daemon.emit_library(&m.id);
    let voices = db.voice_embeddings()?;
    let job = Job { mic_wav: m.mic_wav(), system_wav: m.system_wav(), voices: &voices, threshold };
    let mut last = Instant::now() - Duration::from_secs(1);
    let transcript = engine.transcribe(&job, |p, stage| {
        if last.elapsed() >= Duration::from_millis(500) {
            last = Instant::now();
            report(daemon, db, m, p, stage);
            daemon.emit_library(&m.id);
        }
    })?;
    // `store_transcript` needs `&mut`; use a short-lived connection.
    let mut w = Db::open_default()?;
    w.store_transcript(&m.id, &transcript)?;
    actas_core::export::write_transcript_md(&w, &m.id)?;
    Ok(())
}
