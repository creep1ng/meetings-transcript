//! Colours and small shared pieces, matching the accepted mockup.

use gpui_kit::*;

/// The system monospace family (gpui does not resolve the generic name).
pub fn mono() -> SharedString {
    static MONO: std::sync::OnceLock<SharedString> = std::sync::OnceLock::new();
    MONO.get_or_init(|| {
        std::process::Command::new("fc-match")
            .args(["-f", "%{family[0]}", "monospace"])
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .filter(|f| !f.trim().is_empty())
            .unwrap_or_else(|| "DejaVu Sans Mono".into())
            .into()
    })
    .clone()
}

/// Colours used by Actas' own views; built from the desktop theme
/// (see `theme.rs`) and stored as a gpui global.
#[derive(Clone, Copy)]
pub struct Palette {
    pub bg: Hsla,
    pub card: Hsla,
    pub fg: Hsla,
    pub muted: Hsla,
    pub border: Hsla,
    pub track: Hsla,
    pub accent: Hsla,
    pub rec: Hsla,
    pub ok: Hsla,
    pub warning: Hsla,
    pub danger: Hsla,
    pub info: Hsla,
    /// Speaker colours (index = `Speaker::color`).
    pub speakers: [Hsla; 8],
    /// Noctalia's glass transparency is on.
    pub glass: bool,
}

impl Palette {
    pub fn get(cx: &App) -> Self {
        cx.try_global::<Palette>().copied().unwrap_or_else(|| super::theme::Scheme::default().palette())
    }

    pub fn speaker(&self, i: i64) -> Hsla {
        self.speakers[(i.max(0) as usize) % self.speakers.len()]
    }
}

pub fn rec_dot(p: &Palette, active: bool) -> Div {
    div().size(px(10.)).rounded_full().bg(if active { p.rec } else { p.muted })
}

pub fn pill(p: &Palette, text: impl Into<SharedString>, color: Hsla) -> Div {
    div()
        .px_2()
        .py(px(1.))
        .rounded_full()
        .border_1()
        .border_color(color.opacity(0.5))
        .text_xs()
        .text_color(color)
        .child(text.into())
        .bg(p.card)
}

pub fn progress(p: &Palette, value: f32) -> Div {
    div()
        .h(px(6.))
        .w_full()
        .rounded(px(3.))
        .bg(p.track)
        .child(div().h_full().rounded(px(3.)).bg(p.info).w(relative(value.clamp(0.0, 1.0))))
}
