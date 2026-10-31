//! Unix-socket IPC server (see `actas_core::ipc`).

use actas_core::ipc::{Request, Response};
use actas_core::paths;
use anyhow::{Result, bail};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};

use crate::daemon::Daemon;

pub fn bind() -> Result<UnixListener> {
    let path = paths::socket_path();
    if path.exists() {
        if std::os::unix::net::UnixStream::connect(&path).is_ok() {
            bail!("actasd ya está en marcha ({})", path.display());
        }
        std::fs::remove_file(&path)?;
    }
    Ok(UnixListener::bind(&path)?)
}

pub async fn serve(listener: UnixListener, daemon: Daemon) {
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                let d = daemon.clone();
                tokio::spawn(async move {
                    if let Err(e) = handle(stream, d).await {
                        tracing::debug!("conexión IPC: {e:#}");
                    }
                });
            }
            Err(e) => tracing::warn!("aceptando conexión IPC: {e}"),
        }
    }
}

async fn write_line(w: &mut (impl AsyncWriteExt + Unpin), value: &impl serde::Serialize) -> Result<()> {
    let mut line = serde_json::to_vec(value)?;
    line.push(b'\n');
    w.write_all(&line).await?;
    Ok(())
}

async fn handle(stream: UnixStream, daemon: Daemon) -> Result<()> {
    let (r, mut w) = stream.into_split();
    let mut lines = BufReader::new(r).lines();
    while let Some(line) = lines.next_line().await? {
        let req: Request = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                write_line(&mut w, &Response::Error { message: format!("petición inválida: {e}") }).await?;
                continue;
            }
        };
        if req == Request::Subscribe {
            write_line(&mut w, &Response::Ok).await?;
            let mut rx = daemon.subscribe();
            write_line(&mut w, &actas_core::ipc::Event::Status(daemon.status())).await?;
            loop {
                match rx.recv().await {
                    Ok(ev) => write_line(&mut w, &ev).await?,
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(_) => return Ok(()),
                }
            }
        }
        let resp = dispatch(&daemon, req).await;
        write_line(&mut w, &resp).await?;
    }
    Ok(())
}

async fn dispatch(d: &Daemon, req: Request) -> Response {
    let result: Result<Option<Response>> = async {
        match req {
            Request::Status => return Ok(Some(Response::Status(d.status()))),
            Request::Start { title, video } => d.start(title, video).await?,
            Request::Stop => d.stop().await?,
            Request::Toggle => d.toggle().await?,
            Request::TogglePause => d.toggle_pause()?,
            Request::Mark { label } => d.mark(label)?,
            Request::AcceptSuggestion { video } => d.accept_suggestion(video).await?,
            Request::DismissSuggestion => d.dismiss_suggestion().await,
            Request::Transcribe { meeting_id } => d.enqueue(&meeting_id)?,
            Request::ReloadConfig => d.reload_config()?,
            Request::Quit => {
                let d = d.clone();
                tokio::spawn(async move {
                    d.shutdown().await;
                    std::process::exit(0);
                });
            }
            Request::Subscribe => unreachable!(),
        }
        Ok(None)
    }
    .await;
    match result {
        Ok(Some(r)) => r,
        Ok(None) => Response::Ok,
        Err(e) => Response::Error { message: format!("{e:#}") },
    }
}
