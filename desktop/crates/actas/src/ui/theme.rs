//! Colours from the desktop theme.
//!
//! Noctalia renders `packaging/noctalia-actas-theme.json` into
//! `~/.config/actas/theme.json` every time the palette changes (a user
//! template). Without it, the colours Noctalia writes for Hyprland
//! (`~/.config/hypr/noctalia.lua`) are used; failing that, Catppuccin Mocha.
//! Noctalia's "glass" transparency makes the window translucent so Hyprland
//! blurs behind it.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use gpui_kit::component::Theme;
use gpui_kit::*;

use super::style::Palette;

#[derive(Debug, Clone, PartialEq)]
pub struct Scheme {
    pub surface: u32,
    pub surface_container: u32,
    pub surface_container_highest: u32,
    pub on_surface: u32,
    pub outline: u32,
    pub primary: u32,
    pub on_primary: u32,
    pub secondary: u32,
    pub tertiary: u32,
    pub error: u32,
    pub on_error: u32,
    pub glass: bool,
}

impl Default for Scheme {
    /// Catppuccin Mocha, Noctalia's default palette.
    fn default() -> Self {
        Self {
            surface: 0x1e1e2e,
            surface_container: 0x313244,
            surface_container_highest: 0x42435c,
            on_surface: 0xcdd6f4,
            outline: 0x646883,
            primary: 0xcba6f7,
            on_primary: 0x11111b,
            secondary: 0xfab387,
            tertiary: 0x94e2d5,
            error: 0xf38ba8,
            on_error: 0x11111b,
            glass: false,
        }
    }
}

fn config_home() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config"))
}

pub fn theme_file() -> PathBuf {
    actas_core::paths::config_dir().join("theme.json")
}

fn hex(s: &str) -> Option<u32> {
    let s = s.trim().trim_start_matches('#');
    let s = s.strip_prefix("rgb(").and_then(|r| r.strip_suffix(')')).unwrap_or(s);
    (s.len() >= 6).then(|| u32::from_str_radix(&s[..6], 16).ok()).flatten()
}

fn noctalia_glass() -> bool {
    let state = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state"));
    std::fs::read_to_string(state.join("noctalia/settings.toml"))
        .map(|s| s.lines().any(|l| l.replace(' ', "") == "transparency_mode=\"glass\""))
        .unwrap_or(false)
}

impl Scheme {
    pub fn load() -> Self {
        let mut s = Self::from_template().or_else(Self::from_hypr).unwrap_or_default();
        s.glass = noctalia_glass();
        s
    }

    fn from_template() -> Option<Self> {
        let text = std::fs::read_to_string(theme_file()).ok()?;
        let map: HashMap<String, String> = serde_json::from_str(&text).ok()?;
        let d = Self::default();
        let get = |k: &str, fallback: u32| map.get(k).and_then(|v| hex(v)).unwrap_or(fallback);
        Some(Self {
            surface: get("surface", d.surface),
            surface_container: get("surface_container", d.surface_container),
            surface_container_highest: get("surface_container_highest", d.surface_container_highest),
            on_surface: get("on_surface", d.on_surface),
            outline: get("outline", d.outline),
            primary: get("primary", d.primary),
            on_primary: get("on_primary", d.on_primary),
            secondary: get("secondary", d.secondary),
            tertiary: get("tertiary", d.tertiary),
            error: get("error", d.error),
            on_error: get("on_error", d.on_error),
            glass: false,
        })
    }

    /// `local primary = "rgb(cba6f7)"` lines from Noctalia's Hyprland output.
    fn from_hypr() -> Option<Self> {
        let text = std::fs::read_to_string(config_home().join("hypr/noctalia.lua")).ok()?;
        let mut map = HashMap::new();
        for line in text.lines() {
            let Some(rest) = line.trim().strip_prefix("local ") else { continue };
            let Some((k, v)) = rest.split_once('=') else { continue };
            if let Some(c) = hex(v.trim().trim_matches('"')) {
                map.insert(k.trim().to_string(), c);
            }
        }
        let d = Self::default();
        let surface = *map.get("surface")?;
        Some(Self {
            surface,
            primary: map.get("primary").copied().unwrap_or(d.primary),
            on_surface: map.get("on_surface").copied().unwrap_or(d.on_surface),
            secondary: map.get("secondary").copied().unwrap_or(d.secondary),
            error: map.get("error").copied().unwrap_or(d.error),
            on_error: map.get("on_error").copied().unwrap_or(d.on_error),
            ..d
        })
    }

    pub fn palette(&self) -> Palette {
        let c = |v: u32| -> Hsla { rgb(v).into() };
        let fg = c(self.on_surface);
        Palette {
            bg: c(self.surface).opacity(if self.glass { 0.82 } else { 1.0 }),
            card: c(self.surface_container),
            fg,
            muted: fg.opacity(0.62),
            border: c(self.outline).opacity(0.5),
            track: c(self.surface_container),
            accent: c(self.primary).opacity(0.2),
            rec: c(self.error),
            ok: c(self.tertiary),
            warning: c(self.secondary),
            danger: c(self.error),
            info: c(self.primary),
            speakers: [
                c(self.primary),
                c(self.tertiary),
                c(self.secondary),
                c(self.error),
                rgb(0x89b4fa).into(),
                rgb(0xa6e3a1).into(),
                rgb(0xf9e2af).into(),
                rgb(0x74c7ec).into(),
            ],
            glass: self.glass,
        }
    }
}

/// Installs the scheme: our palette global plus gpui-component's colours.
pub fn apply(scheme: &Scheme, cx: &mut App) {
    let p = scheme.palette();
    let c = |v: u32| -> Hsla { rgb(v).into() };
    let (primary, on_primary, error, on_error) =
        (c(scheme.primary), c(scheme.on_primary), c(scheme.error), c(scheme.on_error));
    let (container, highest) = (c(scheme.surface_container), c(scheme.surface_container_highest));
    Theme::update(cx, |t| {
        let k = &mut t.colors;
        k.background = p.bg;
        k.foreground = p.fg;
        k.border = p.border;
        k.input = p.border;
        k.ring = primary;
        k.caret = primary;
        k.selection = primary.opacity(0.3);
        k.muted = container;
        k.muted_foreground = p.muted;
        k.accent = highest;
        k.accent_foreground = p.fg;
        k.primary = primary;
        k.primary_hover = primary.opacity(0.88);
        k.primary_active = primary.opacity(0.76);
        k.primary_foreground = on_primary;
        k.button_primary = primary;
        k.button_primary_hover = primary.opacity(0.88);
        k.button_primary_active = primary.opacity(0.76);
        k.button_primary_foreground = on_primary;
        k.secondary = container;
        k.secondary_hover = highest;
        k.secondary_active = highest;
        k.secondary_foreground = p.fg;
        k.button = container;
        k.button_hover = highest;
        k.button_active = highest;
        k.button_foreground = p.fg;
        k.danger = error;
        k.danger_hover = error.opacity(0.88);
        k.danger_active = error.opacity(0.76);
        k.danger_foreground = on_error;
        k.button_danger = error;
        k.button_danger_hover = error.opacity(0.88);
        k.button_danger_active = error.opacity(0.76);
        k.button_danger_foreground = on_error;
        k.popover = container;
        k.popover_foreground = p.fg;
        k.list = transparent_black();
        k.list_hover = container;
        k.list_active = p.accent;
        k.list_active_border = primary;
        k.switch = highest;
        k.tab_bar = transparent_black();
        k.tab_active_foreground = p.fg;
        k.tab_foreground = p.muted;
        k.title_bar = transparent_black();
        k.title_bar_border = p.border;
        k.scrollbar = transparent_black();
        k.scrollbar_thumb = c(scheme.outline).opacity(0.6);
        k.scrollbar_thumb_hover = c(scheme.outline);
        k.progress_bar = primary;
        k.overlay = black().opacity(0.4);
        k.window_border = p.border;
    });
    cx.set_global(p);
}

impl Global for Palette {}

fn mtime() -> Option<SystemTime> {
    std::fs::metadata(theme_file()).and_then(|m| m.modified()).ok()
}

/// Applies the current scheme and re-applies it whenever Noctalia rewrites
/// the theme file.
pub fn init(cx: &mut App) {
    apply(&Scheme::load(), cx);
    cx.spawn(async move |cx| {
        let mut last = mtime();
        loop {
            cx.background_executor().timer(Duration::from_millis(1500)).await;
            let now = mtime();
            if now != last {
                last = now;
                cx.update(|cx| {
                    apply(&Scheme::load(), cx);
                    cx.refresh_windows();
                });
            }
        }
    })
    .detach();
}

#[cfg(test)]
mod tests {
    // Not `super::*`: gpui exports its own `test` attribute.
    use super::hex;

    #[test]
    fn parses_colours() {
        assert_eq!(hex("#cba6f7"), Some(0xcba6f7));
        assert_eq!(hex("rgb(1e1e2e)"), Some(0x1e1e2e));
        assert_eq!(hex("nope"), None);
    }
}
