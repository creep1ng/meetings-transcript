//! Shared types for the Actas daemon, GUI and CLI: paths, configuration,
//! the SQLite library and the IPC protocol.

pub mod config;
pub mod db;
pub mod export;
pub mod ipc;
pub mod paths;
pub mod voice;

pub use config::Config;
pub use db::Db;

/// Formats milliseconds as `H:MM:SS` (or `MM:SS` under one hour).
pub fn fmt_ms(ms: i64) -> String {
    let total = ms.max(0) / 1000;
    let (h, m, s) = (total / 3600, (total / 60) % 60, total % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m:02}:{s:02}") }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_durations() {
        assert_eq!(fmt_ms(0), "00:00");
        assert_eq!(fmt_ms(61_500), "01:01");
        assert_eq!(fmt_ms(3_723_000), "1:02:03");
    }
}
