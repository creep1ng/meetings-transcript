//! Launching things outside Actas: Codex in a terminal, the video player, the
//! file manager.

use std::path::Path;
use std::process::Command;

use actas_core::config::Summary;
use actas_core::db::Meeting;
use anyhow::{Context, Result};

fn spawn_detached(cmd: &mut Command) -> Result<()> {
    let mut child = cmd.spawn().with_context(|| format!("no pude ejecutar {:?}", cmd.get_program()))?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

/// Opens an interactive Codex session in a terminal, in the meeting folder,
/// with the configured first prompt. The Notion destination is decided in
/// that conversation. This is the only path by which a transcript leaves
/// the machine, and it only runs when the user asks.
pub fn open_codex(summary: &Summary, meeting: &Meeting) -> Result<()> {
    let transcript = meeting.dir.join("transcript.md");
    if !transcript.exists() {
        anyhow::bail!("todavía no hay transcripción");
    }
    let prompt =
        summary.prompt.replace("{transcript}", &transcript.to_string_lossy()).replace("{title}", &meeting.title);
    let (term, term_args) = summary.terminal.split_first().context("no hay terminal configurada")?;
    // The terminal inherits the working directory; the remaining arguments are
    // the command to run (kitty, foot, alacritty, wezterm `start` all accept this).
    let mut cmd = Command::new(term);
    cmd.args(term_args).current_dir(&meeting.dir).arg(&summary.codex).arg(prompt);
    spawn_detached(&mut cmd)
}

/// Opens the recording in the default video player, at `at_ms` when the
/// player is mpv.
pub fn open_video(path: &Path, at_ms: i64) -> Result<()> {
    if which("mpv") {
        return spawn_detached(Command::new("mpv").arg(format!("--start={}", at_ms as f64 / 1000.0)).arg(path));
    }
    spawn_detached(Command::new("xdg-open").arg(path))
}

pub fn open_folder(path: &Path) -> Result<()> {
    spawn_detached(Command::new("xdg-open").arg(path))
}

fn which(bin: &str) -> bool {
    std::env::var_os("PATH").map(|p| std::env::split_paths(&p).any(|d| d.join(bin).is_file())).unwrap_or(false)
}
