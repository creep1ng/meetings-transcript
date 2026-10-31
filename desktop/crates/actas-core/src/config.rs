//! `~/.config/actas/config.toml`. Every field has a default, so a missing or
//! partial file is valid. The GUI rewrites the whole file when settings change.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::paths;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub detection: Detection,
    pub capture: Capture,
    pub transcription: Transcription,
    pub voices: Voices,
    pub summary: Summary,
    pub storage: Storage,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum OnDetect {
    /// Send a notification with "Grabar / Solo audio / Ahora no".
    Suggest,
    /// Start recording without asking.
    Record,
    Ignore,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Detection {
    /// Watch PipeWire for audio capture streams opened by `apps`.
    pub pipewire_mic: bool,
    /// Read Hyprland window titles to recognise and name the meeting.
    pub window_title: bool,
    /// Substrings matched (case-insensitive) against the PipeWire
    /// `application.process.binary` / `application.name` of a capture stream.
    pub apps: Vec<String>,
    pub title_patterns: Vec<TitlePattern>,
    pub on_detect: OnDetect,
    /// Window titles that always start a recording (from "Grabar siempre que
    /// coincida este título").
    pub auto_record_titles: Vec<String>,
    /// Apps for which the user chose "No sugerir con esta app".
    pub muted_apps: Vec<String>,
    /// Seconds a capture stream must stay open before it counts as a call.
    pub debounce_secs: u64,
    /// After "Ahora no", seconds before suggesting again for the same call.
    pub snooze_secs: u64,
    /// Seconds without the call's capture stream before suggesting to stop.
    pub end_grace_secs: u64,
}

impl Default for Detection {
    fn default() -> Self {
        Self {
            pipewire_mic: true,
            window_title: true,
            apps: ["brave", "firefox", "chromium", "chrome", "vivaldi", "zen", "zoom", "teams", "discord", "slack"]
                .map(String::from)
                .to_vec(),
            title_patterns: TitlePattern::defaults(),
            on_detect: OnDetect::Suggest,
            auto_record_titles: Vec::new(),
            muted_apps: Vec::new(),
            debounce_secs: 5,
            snooze_secs: 600,
            end_grace_secs: 45,
        }
    }
}

/// A window-title regex. The named group `title`, if present, becomes the
/// recording title.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TitlePattern {
    pub platform: String,
    pub regex: String,
}

impl TitlePattern {
    pub fn defaults() -> Vec<Self> {
        [
            ("Meet", r"^Meet\s*[-–]\s*(?P<title>.+?)(\s+[-–]\s+[^-–]+)?$"),
            ("Teams", r"^(?P<title>.+?)\s*\|\s*Microsoft Teams"),
            ("Zoom", r"^Zoom( Meeting| Workplace)?$"),
            ("Jitsi", r"^(?P<title>.+?)\s*\|\s*Jitsi Meet"),
        ]
        .into_iter()
        .map(|(platform, regex)| Self { platform: platform.into(), regex: regex.into() })
        .collect()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum VideoEncoder {
    /// First available of NVENC → VA-API → x264.
    Auto,
    NvencH264,
    NvencH265,
    NvencAv1,
    VaapiH264,
    X264,
}

impl VideoEncoder {
    pub const ALL: [Self; 6] =
        [Self::Auto, Self::NvencH264, Self::NvencH265, Self::NvencAv1, Self::VaapiH264, Self::X264];

    pub fn label(self) -> &'static str {
        match self {
            Self::Auto => "Automático (NVENC → VA-API → software)",
            Self::NvencH264 => "NVENC H.264",
            Self::NvencH265 => "NVENC H.265",
            Self::NvencAv1 => "NVENC AV1",
            Self::VaapiH264 => "VA-API H.264",
            Self::X264 => "Software (x264)",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Capture {
    pub encoder: VideoEncoder,
    pub fps: u32,
    /// Reuse the window chosen in the portal dialog (`restore_token`).
    pub remember_source: bool,
    /// PulseAudio/PipeWire source names; empty = system default.
    pub mic_device: String,
    pub monitor_device: String,
}

impl Default for Capture {
    fn default() -> Self {
        Self {
            encoder: VideoEncoder::Auto,
            fps: 15,
            remember_source: true,
            mic_device: String::new(),
            monitor_device: String::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Device {
    Cuda,
    Cpu,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Transcription {
    pub device: Device,
    /// Directory with `encoder-model.onnx`, `decoder_joint-model.onnx`, `vocab.txt`.
    pub asr_model: PathBuf,
    pub diarization_model: PathBuf,
    pub embedding_model: PathBuf,
    /// Unload the models after this many idle seconds to free VRAM.
    pub unload_after_secs: u64,
}

impl Default for Transcription {
    fn default() -> Self {
        let m = paths::models_dir();
        Self {
            device: Device::Cuda,
            asr_model: m.join("parakeet-tdt-0.6b-v3"),
            diarization_model: m.join("nemotron3_diar_v3.onnx"),
            embedding_model: m.join("wespeaker_en_voxceleb_CAM++.onnx"),
            unload_after_secs: 300,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Voices {
    /// Cosine similarity needed to name a speaker after a saved voice.
    pub threshold: f32,
}

impl Default for Voices {
    fn default() -> Self {
        Self { threshold: 0.75 }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Summary {
    /// Terminal that hosts the interactive Codex session. `{cmd}` is replaced
    /// by the argv to run.
    pub terminal: Vec<String>,
    pub codex: String,
    /// First message sent to Codex. `{transcript}` and `{title}` are replaced.
    pub prompt: String,
}

impl Default for Summary {
    fn default() -> Self {
        Self {
            terminal: vec!["kitty".into(), "--title".into(), "Actas · Codex".into()],
            codex: "codex".into(),
            prompt: "Lee la transcripción de la reunión «{title}» en {transcript}. \
                     Resúmela (decisiones, acciones con responsable y fecha, temas abiertos) \
                     y pregúntame en qué página o base de Notion la guardo antes de escribir nada."
                .into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Storage {
    pub recordings_dir: PathBuf,
}

impl Default for Storage {
    fn default() -> Self {
        Self { recordings_dir: paths::default_recordings_dir() }
    }
}

impl Config {
    pub fn load() -> Result<Self> {
        Self::load_from(&paths::config_file())
    }

    pub fn load_from(path: &Path) -> Result<Self> {
        match std::fs::read_to_string(path) {
            Ok(text) => toml::from_str(&text).with_context(|| format!("parsing {}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
        }
    }

    /// Loads the config, falling back to defaults (with a warning) if the file is broken.
    pub fn load_or_default() -> Self {
        Self::load().unwrap_or_else(|e| {
            tracing::warn!("config inválida, uso valores por defecto: {e:#}");
            Self::default()
        })
    }

    pub fn save(&self) -> Result<()> {
        self.save_to(&paths::config_file())
    }

    /// Writes atomically (temp file + rename).
    pub fn save_to(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("toml.tmp");
        std::fs::write(&tmp, toml::to_string_pretty(self)?)?;
        std::fs::rename(&tmp, path)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn partial_file_keeps_defaults() {
        let cfg: Config = toml::from_str("[capture]\nencoder = \"x264\"\n").unwrap();
        assert_eq!(cfg.capture.encoder, VideoEncoder::X264);
        assert_eq!(cfg.capture.fps, 15);
        assert!(cfg.detection.pipewire_mic);
    }

    #[test]
    fn round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("c.toml");
        let mut cfg = Config::default();
        cfg.voices.threshold = 0.8;
        cfg.save_to(&path).unwrap();
        assert_eq!(Config::load_from(&path).unwrap(), cfg);
    }

    #[test]
    fn default_patterns_compile_and_match() {
        let pats: Vec<_> =
            TitlePattern::defaults().into_iter().map(|p| (p.platform, regex::Regex::new(&p.regex).unwrap())).collect();
        let find = |t: &str| {
            pats.iter()
                .find(|(_, r)| r.is_match(t))
                .map(|(p, r)| (p.clone(), r.captures(t).and_then(|c| c.name("title")).map(|m| m.as_str().to_string())))
        };
        assert_eq!(
            find("Meet – Weekly sync plataforma - Brave"),
            Some(("Meet".into(), Some("Weekly sync plataforma".into())))
        );
        assert_eq!(find("Meet - abc-defg-hij"), Some(("Meet".into(), Some("abc-defg-hij".into()))));
        assert_eq!(find("Daily | Microsoft Teams - Brave"), Some(("Teams".into(), Some("Daily".into()))));
        assert_eq!(find("Zoom Meeting"), Some(("Zoom".into(), None)));
        assert_eq!(find("Inbox - Gmail - Brave"), None);
    }
}
