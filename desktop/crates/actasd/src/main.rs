//! `actasd`: detects calls, records screen + audio and transcribes locally.

mod daemon;
mod detector;
mod notify;
mod portal;
mod recorder;
mod server;
mod tray;
mod worker;

use std::time::Duration;

use actas_core::{Config, Db};
use anyhow::Result;
use ksni::TrayMethods;

use crate::daemon::Daemon;

fn main() -> Result<()> {
    if std::env::args().any(|a| a == "--version" || a == "-V") {
        println!("actasd {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info,zbus=warn".into()),
        )
        .init();
    recorder::init()?;
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build()?;
    rt.block_on(run(rt.handle().clone()))
}

async fn run(rt: tokio::runtime::Handle) -> Result<()> {
    let listener = server::bind()?;
    let config = Config::load_or_default();
    let db = Db::open_default()?;
    db.recover_interrupted()?;
    let notifier = match notify::Notifier::connect().await {
        Ok(n) => Some(n),
        Err(e) => {
            tracing::warn!("sin notificaciones: {e:#}");
            None
        }
    };
    let (worker_tx, worker_rx) = std::sync::mpsc::channel();
    let daemon = Daemon::new(config, db, notifier.clone(), worker_tx, rt);

    // Transcription queue.
    {
        let d = daemon.clone();
        std::thread::Builder::new().name("actas-worker".into()).spawn(move || worker::run(d, worker_rx))?;
    }

    // Tray icon.
    match (tray::Tray { daemon: daemon.clone(), status: daemon.status() }).assume_sni_available(true).spawn().await {
        Ok(handle) => daemon.set_tray(handle),
        Err(e) => tracing::warn!("sin icono en la bandeja: {e}"),
    }

    // Notification actions.
    if let Some(n) = notifier {
        let d = daemon.clone();
        tokio::spawn(async move {
            let rt = tokio::runtime::Handle::current();
            let res = n
                .listen(move |id, key| {
                    let d = d.clone();
                    rt.spawn(async move { d.on_notification_action(id, key).await });
                })
                .await;
            if let Err(e) = res {
                tracing::warn!("escuchando acciones de notificaciones: {e:#}");
            }
        });
    }

    // Detection every 2 s; status pushes while recording.
    {
        let d = daemon.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(2));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tick.tick().await;
                d.detect_once().await;
            }
        });
    }
    {
        let d = daemon.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_millis(250));
            loop {
                tick.tick().await;
                if d.is_recording() {
                    d.emit_status();
                }
            }
        });
    }

    // Warm up the resident UI process so the first click opens instantly.
    tray::launch(&["--background"]);

    tracing::info!("actasd en marcha ({})", actas_core::paths::socket_path().display());
    daemon.emit_status();

    let serve = server::serve(listener, daemon.clone());
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        _ = serve => {}
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
    tracing::info!("cerrando actasd");
    daemon.shutdown().await;
    Ok(())
}
