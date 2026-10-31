//! Screen capture through `xdg-desktop-portal` (ScreenCast). The portal shows
//! its picker the first time; the returned `restore_token` is saved so later
//! recordings reuse the same window or screen without asking.

use std::path::PathBuf;

use anyhow::{Context, Result};
use ashpd::desktop::PersistMode;
use ashpd::desktop::screencast::{CursorMode, Screencast, SelectSourcesOptions, SourceType};

use crate::recorder::VideoSource;

fn token_file() -> PathBuf {
    actas_core::paths::state_dir().join("screencast-restore-token")
}

/// Forgets the saved source so the next recording shows the picker again.
pub fn forget_source() {
    let _ = std::fs::remove_file(token_file());
}

/// An open portal session. Dropping it without [`Session::close`] leaves the
/// portal to clean up when the D-Bus connection goes away.
pub struct Session {
    proxy: Screencast,
    session: ashpd::desktop::Session<Screencast>,
}

impl Session {
    pub async fn close(self) {
        if let Err(e) = self.session.close().await {
            tracing::debug!("cerrando la sesión del portal: {e}");
        }
        drop(self.proxy);
    }
}

/// [`open`], retried twice. The Hyprland portal backend starts on demand and
/// the first request can fail with "Invalid session" while it initialises.
/// A cancelled picker is not retried.
pub async fn open_with_retry(remember: bool) -> Result<(Session, VideoSource)> {
    let mut attempt = 0;
    loop {
        match open(remember).await {
            Ok(r) => return Ok(r),
            Err(e) if attempt < 2 && !is_cancelled(&e) => {
                attempt += 1;
                tracing::info!("el portal falló ({e:#}); reintento {attempt}/2");
                tokio::time::sleep(std::time::Duration::from_millis(700)).await;
            }
            Err(e) => return Err(e),
        }
    }
}

fn is_cancelled(e: &anyhow::Error) -> bool {
    e.chain().any(|c| {
        matches!(
            c.downcast_ref::<ashpd::Error>(),
            Some(ashpd::Error::Response(ashpd::desktop::ResponseError::Cancelled))
        )
    })
}

pub async fn open(remember: bool) -> Result<(Session, VideoSource)> {
    let proxy = Screencast::new().await.context("el portal ScreenCast no está disponible")?;
    let session = proxy.create_session(Default::default()).await?;
    let saved = if remember { std::fs::read_to_string(token_file()).ok() } else { None };
    let sources = proxy.available_source_types().await.unwrap_or(SourceType::Monitor | SourceType::Window);
    let cursor = proxy
        .available_cursor_modes()
        .await
        .ok()
        .filter(|m| m.contains(CursorMode::Embedded))
        .map(|_| CursorMode::Embedded)
        .unwrap_or(CursorMode::Hidden);
    proxy
        .select_sources(
            &session,
            SelectSourcesOptions::default()
                .set_cursor_mode(cursor)
                .set_sources(sources & (SourceType::Monitor | SourceType::Window))
                .set_multiple(false)
                .set_restore_token(saved.as_deref().map(str::trim))
                .set_persist_mode(if remember { PersistMode::ExplicitlyRevoked } else { PersistMode::DoNot }),
        )
        .await
        .context("eligiendo qué capturar")?;
    let response =
        proxy.start(&session, None, Default::default()).await?.response().context("captura de pantalla cancelada")?;
    if remember && let Some(token) = response.restore_token() {
        let path = token_file();
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Err(e) = std::fs::write(&path, token) {
            tracing::warn!("no pude guardar el token del portal: {e}");
        }
    }
    let stream = response.streams().first().cloned().context("el portal no devolvió ninguna fuente")?;
    let fd = proxy.open_pipe_wire_remote(&session, Default::default()).await?;
    let kind = match stream.source_type() {
        Some(SourceType::Window) => "Ventana",
        Some(SourceType::Monitor) => "Pantalla",
        _ => "Fuente",
    };
    let label = match stream.size() {
        Some((w, h)) => format!("{kind} {w}×{h}"),
        None => kind.to_string(),
    };
    Ok((Session { proxy, session }, VideoSource { fd, node_id: stream.pipe_wire_node_id(), label }))
}
