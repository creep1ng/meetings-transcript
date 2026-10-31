//! Call detection from two signals:
//!
//! 1. **PipeWire**: an audio capture stream (`Stream/Input/Audio`) opened by
//!    one of the configured apps (browsers, Zoom, Teams…).
//! 2. **Window title** (Hyprland IPC): recognises the platform and gives the
//!    recording a name.
//!
//! [`Detector`] is a pure state machine fed with [`Observation`]s, so the
//! timing rules are testable without PipeWire or Hyprland.

use std::process::Command;
use std::time::{Duration, Instant};

use actas_core::config::{Detection, OnDetect, TitlePattern};
use regex::Regex;
use serde_json::Value;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Observation {
    /// Apps with an open capture stream (matched names from the config).
    pub capture_apps: Vec<String>,
    /// First window whose title matches a meeting pattern.
    pub window: Option<WindowMatch>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct WindowMatch {
    pub platform: String,
    pub title: Option<String>,
    pub raw_title: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Call {
    pub app: String,
    pub platform: Option<String>,
    pub title: Option<String>,
    pub raw_title: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Suggest(Call),
    AutoRecord(Call),
    /// The call disappeared before the user answered the suggestion.
    Withdraw,
    /// The call that triggered the current recording seems over.
    CallEnded,
}

/// Grace period before an unanswered suggestion is withdrawn.
const WITHDRAW_AFTER: Duration = Duration::from_secs(5);

#[derive(Debug, Default)]
pub struct Detector {
    active_since: Option<Instant>,
    missing_since: Option<Instant>,
    current: Option<Call>,
    announced: bool,
    snoozed_until: Option<Instant>,
    ended_reported: bool,
}

impl Detector {
    pub fn current(&self) -> Option<&Call> {
        self.current.as_ref()
    }

    /// "Ahora no": suggest again after `snooze_secs` if the call goes on.
    pub fn snooze(&mut self, now: Instant, cfg: &Detection) {
        self.snoozed_until = Some(now + Duration::from_secs(cfg.snooze_secs));
        self.announced = false;
    }

    /// A recording started for the current call (by any means).
    pub fn recording_started(&mut self) {
        self.announced = true;
        self.ended_reported = false;
    }

    pub fn observe(&mut self, now: Instant, obs: &Observation, recording: bool, cfg: &Detection) -> Vec<Action> {
        let apps: Vec<&String> =
            obs.capture_apps.iter().filter(|a| !cfg.muted_apps.iter().any(|m| m.eq_ignore_ascii_case(a))).collect();
        let present = if cfg.pipewire_mic { !apps.is_empty() } else { cfg.window_title && obs.window.is_some() };
        let mut actions = Vec::new();

        if present {
            self.missing_since = None;
            let since = *self.active_since.get_or_insert(now);
            let window = if cfg.window_title { obs.window.as_ref() } else { None };
            let call = Call {
                app: apps.first().map(|a| a.to_string()).unwrap_or_else(|| "ventana".into()),
                platform: window.map(|w| w.platform.clone()),
                title: window.and_then(|w| w.title.clone()),
                raw_title: window.map(|w| w.raw_title.clone()),
            };
            self.current = Some(call.clone());
            let snoozed = self.snoozed_until.is_some_and(|t| now < t);
            if !self.announced
                && !recording
                && !snoozed
                && now.duration_since(since) >= Duration::from_secs(cfg.debounce_secs)
            {
                self.announced = true;
                let auto = cfg.on_detect == OnDetect::Record
                    || call
                        .title
                        .as_ref()
                        .or(call.raw_title.as_ref())
                        .is_some_and(|t| cfg.auto_record_titles.iter().any(|a| a.eq_ignore_ascii_case(t)));
                match (auto, cfg.on_detect) {
                    (true, _) => actions.push(Action::AutoRecord(call)),
                    (false, OnDetect::Suggest) => actions.push(Action::Suggest(call)),
                    (false, _) => {}
                }
            }
        } else if self.active_since.is_some() {
            let missing = *self.missing_since.get_or_insert(now);
            let gone_for = now.duration_since(missing);
            if recording {
                if !self.ended_reported && gone_for >= Duration::from_secs(cfg.end_grace_secs) {
                    self.ended_reported = true;
                    actions.push(Action::CallEnded);
                }
            } else if gone_for >= WITHDRAW_AFTER {
                if self.announced {
                    actions.push(Action::Withdraw);
                }
                *self = Self { snoozed_until: self.snoozed_until, ..Self::default() };
            }
        }
        actions
    }
}

/// Apps (from `apps`) that currently hold an audio capture stream, from `pw-dump` JSON.
pub fn capture_apps(pw_dump: &Value, apps: &[String]) -> Vec<String> {
    let mut found = Vec::new();
    let own_pid = std::process::id().to_string();
    for obj in pw_dump.as_array().into_iter().flatten() {
        if obj["type"] != "PipeWire:Interface:Node" {
            continue;
        }
        let props = &obj["info"]["props"];
        if props["media.class"] != "Stream/Input/Audio" {
            continue;
        }
        let pid = match &props["application.process.id"] {
            Value::Number(n) => n.to_string(),
            Value::String(s) => s.clone(),
            _ => String::new(),
        };
        if pid == own_pid {
            continue;
        }
        let names = [&props["application.process.binary"], &props["application.name"]]
            .iter()
            .filter_map(|v| v.as_str())
            .map(str::to_lowercase)
            .collect::<Vec<_>>();
        if let Some(app) = apps.iter().find(|a| names.iter().any(|n| n.contains(&a.to_lowercase())))
            && !found.contains(app)
        {
            found.push(app.clone());
        }
    }
    found
}

pub struct TitleMatcher {
    patterns: Vec<(String, Regex)>,
}

impl TitleMatcher {
    pub fn new(patterns: &[TitlePattern]) -> Self {
        let patterns = patterns
            .iter()
            .filter_map(|p| match Regex::new(&p.regex) {
                Ok(r) => Some((p.platform.clone(), r)),
                Err(e) => {
                    tracing::warn!("patrón de título inválido para {}: {e}", p.platform);
                    None
                }
            })
            .collect();
        Self { patterns }
    }

    pub fn find<'a>(&self, titles: impl IntoIterator<Item = &'a str>) -> Option<WindowMatch> {
        for t in titles {
            for (platform, re) in &self.patterns {
                if let Some(c) = re.captures(t) {
                    return Some(WindowMatch {
                        platform: platform.clone(),
                        title: c.name("title").map(|m| m.as_str().trim().to_string()).filter(|s| !s.is_empty()),
                        raw_title: t.to_string(),
                    });
                }
            }
        }
        None
    }
}

/// Titles of every Hyprland window, focused first.
pub fn hyprland_titles() -> Vec<String> {
    let json = |args: &[&str]| -> Option<Value> {
        let out = Command::new("hyprctl").args(args).output().ok()?;
        serde_json::from_slice(&out.stdout).ok()
    };
    let mut titles = Vec::new();
    if let Some(active) = json(&["activewindow", "-j"])
        && let Some(t) = active["title"].as_str()
    {
        titles.push(t.to_string());
    }
    if let Some(Value::Array(clients)) = json(&["clients", "-j"]) {
        titles.extend(clients.iter().filter_map(|c| c["title"].as_str()).map(String::from));
    }
    titles
}

pub fn pw_dump() -> Option<Value> {
    let out = Command::new("pw-dump").arg("--no-colors").output().ok()?;
    serde_json::from_slice(&out.stdout).ok()
}

/// Takes one observation of the system (blocking: runs `pw-dump` and `hyprctl`).
pub fn observe_system(cfg: &Detection, matcher: &TitleMatcher) -> Observation {
    let capture_apps =
        if cfg.pipewire_mic { pw_dump().map(|d| capture_apps(&d, &cfg.apps)).unwrap_or_default() } else { Vec::new() };
    let window = if cfg.window_title {
        let titles = hyprland_titles();
        matcher.find(titles.iter().map(String::as_str))
    } else {
        None
    };
    Observation { capture_apps, window }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn cfg() -> Detection {
        Detection { debounce_secs: 5, end_grace_secs: 30, snooze_secs: 60, ..Detection::default() }
    }

    fn obs(app: Option<&str>, title: Option<&str>) -> Observation {
        Observation {
            capture_apps: app.map(|a| vec![a.to_string()]).unwrap_or_default(),
            window: title.map(|t| WindowMatch {
                platform: "Meet".into(),
                title: Some(t.into()),
                raw_title: format!("Meet - {t}"),
            }),
        }
    }

    #[test]
    fn suggests_after_debounce_once() {
        let (cfg, t0) = (cfg(), Instant::now());
        let mut d = Detector::default();
        let o = obs(Some("brave"), Some("Weekly"));
        assert!(d.observe(t0, &o, false, &cfg).is_empty());
        assert!(d.observe(t0 + Duration::from_secs(3), &o, false, &cfg).is_empty());
        let a = d.observe(t0 + Duration::from_secs(6), &o, false, &cfg);
        assert!(matches!(&a[..], [Action::Suggest(c)] if c.title.as_deref() == Some("Weekly") && c.app == "brave"));
        assert!(d.observe(t0 + Duration::from_secs(9), &o, false, &cfg).is_empty(), "only once");
    }

    #[test]
    fn short_mic_use_is_ignored_and_unanswered_suggestion_withdrawn() {
        let (cfg, t0) = (cfg(), Instant::now());
        let mut d = Detector::default();
        d.observe(t0, &obs(Some("brave"), None), false, &cfg);
        assert!(d.observe(t0 + Duration::from_secs(2), &obs(None, None), false, &cfg).is_empty());
        assert!(d.observe(t0 + Duration::from_secs(8), &obs(None, None), false, &cfg).is_empty());
        // A real call, suggested, then it ends.
        let t1 = t0 + Duration::from_secs(20);
        d.observe(t1, &obs(Some("brave"), None), false, &cfg);
        assert_eq!(d.observe(t1 + Duration::from_secs(5), &obs(Some("brave"), None), false, &cfg).len(), 1);
        d.observe(t1 + Duration::from_secs(6), &obs(None, None), false, &cfg);
        assert_eq!(d.observe(t1 + Duration::from_secs(12), &obs(None, None), false, &cfg), vec![Action::Withdraw]);
    }

    #[test]
    fn auto_record_rules_and_muted_apps() {
        let t0 = Instant::now();
        let mut cfg = cfg();
        cfg.auto_record_titles = vec!["weekly".into()];
        let mut d = Detector::default();
        let o = obs(Some("brave"), Some("Weekly"));
        d.observe(t0, &o, false, &cfg);
        assert!(matches!(d.observe(t0 + Duration::from_secs(5), &o, false, &cfg)[..], [Action::AutoRecord(_)]));

        cfg.muted_apps = vec!["Brave".into()];
        let mut d = Detector::default();
        d.observe(t0, &o, false, &cfg);
        assert!(d.observe(t0 + Duration::from_secs(10), &o, false, &cfg).is_empty());
    }

    #[test]
    fn reports_end_of_call_while_recording() {
        let (cfg, t0) = (cfg(), Instant::now());
        let mut d = Detector::default();
        let o = obs(Some("brave"), None);
        d.observe(t0, &o, false, &cfg);
        d.observe(t0 + Duration::from_secs(5), &o, false, &cfg);
        d.recording_started();
        let gone = obs(None, None);
        assert!(d.observe(t0 + Duration::from_secs(60), &gone, true, &cfg).is_empty());
        assert_eq!(d.observe(t0 + Duration::from_secs(91), &gone, true, &cfg), vec![Action::CallEnded]);
        assert!(d.observe(t0 + Duration::from_secs(95), &gone, true, &cfg).is_empty(), "once");
    }

    #[test]
    fn snooze_delays_the_next_suggestion() {
        let (cfg, t0) = (cfg(), Instant::now());
        let mut d = Detector::default();
        let o = obs(Some("zoom"), None);
        d.observe(t0, &o, false, &cfg);
        assert_eq!(d.observe(t0 + Duration::from_secs(5), &o, false, &cfg).len(), 1);
        d.snooze(t0 + Duration::from_secs(6), &cfg);
        assert!(d.observe(t0 + Duration::from_secs(30), &o, false, &cfg).is_empty());
        assert_eq!(d.observe(t0 + Duration::from_secs(70), &o, false, &cfg).len(), 1);
    }

    #[test]
    fn parses_pw_dump() {
        let dump = json!([
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "media.class": "Stream/Input/Audio", "application.process.binary": "brave", "application.process.id": 4242}}},
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "media.class": "Stream/Output/Audio", "application.process.binary": "firefox"}}},
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "media.class": "Stream/Input/Audio", "application.name": "ZOOM VoiceEngine"}}},
            {"type": "PipeWire:Interface:Client", "info": {"props": {}}}
        ]);
        let apps: Vec<String> = ["brave", "firefox", "zoom"].map(String::from).to_vec();
        assert_eq!(capture_apps(&dump, &apps), vec!["brave".to_string(), "zoom".to_string()]);
    }

    #[test]
    fn matches_titles() {
        let m = TitleMatcher::new(&TitlePattern::defaults());
        let w = m.find(["YouTube - Brave", "Meet - abc-defg-hij - Brave"]).unwrap();
        assert_eq!((w.platform.as_str(), w.title.as_deref()), ("Meet", Some("abc-defg-hij")));
        assert!(m.find(["(79) Algo - YouTube - Brave"]).is_none());
    }
}
