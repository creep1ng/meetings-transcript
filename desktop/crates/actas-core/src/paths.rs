//! XDG locations. Everything Actas writes lives under these directories.

use std::path::PathBuf;

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/tmp"))
}

fn xdg(var: &str, fallback: &str) -> PathBuf {
    std::env::var_os(var).map(PathBuf::from).filter(|p| p.is_absolute()).unwrap_or_else(|| home().join(fallback))
}

/// `~/.config/actas`
pub fn config_dir() -> PathBuf {
    xdg("XDG_CONFIG_HOME", ".config").join("actas")
}

pub fn config_file() -> PathBuf {
    config_dir().join("config.toml")
}

/// `~/.local/share/actas`
pub fn data_dir() -> PathBuf {
    xdg("XDG_DATA_HOME", ".local/share").join("actas")
}

pub fn db_file() -> PathBuf {
    data_dir().join("actas.db")
}

pub fn default_recordings_dir() -> PathBuf {
    data_dir().join("recordings")
}

pub fn models_dir() -> PathBuf {
    data_dir().join("models")
}

/// `~/.local/state/actas` (portal restore token, logs).
pub fn state_dir() -> PathBuf {
    xdg("XDG_STATE_HOME", ".local/state").join("actas")
}

/// Unix socket of the daemon.
pub fn socket_path() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from).unwrap_or_else(std::env::temp_dir).join("actas.sock")
}
