//! gpui windows: the main library window and the recording panel.
//!
//! One resident UI process serves every window. Starting gpui costs a second
//! or more (font scan, GPU setup; several seconds with a cold disk cache), so
//! `actas` and `actas popover` first try `$XDG_RUNTIME_DIR/actas-ui.sock`: if a
//! UI process is listening they hand it the request and exit at once. The
//! daemon starts `actas --background` at login so the first click is fast too.

mod main_window;
mod popover;
mod settings;
pub mod style;
mod theme;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};

use anyhow::Result;
use gpui_kit::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Open {
    Main,
    /// Toggles the recording panel.
    Popover,
}

impl Open {
    fn as_str(self) -> &'static str {
        match self {
            Open::Main => "main",
            Open::Popover => "popover",
        }
    }

    fn parse(s: &str) -> Option<Self> {
        match s.trim() {
            "main" => Some(Open::Main),
            "popover" => Some(Open::Popover),
            _ => None,
        }
    }
}

gpui_kit::actions!(actas, [ClosePanel]);

fn ui_socket() -> std::path::PathBuf {
    actas_core::paths::socket_path().with_file_name("actas-ui.sock")
}

/// Open windows of the resident process.
#[derive(Default)]
struct Windows {
    main: Option<AnyWindowHandle>,
    popover: Option<AnyWindowHandle>,
}

impl Global for Windows {}

/// Regenerates `transcript.md` after a rename.
pub fn rewrite_markdown(id: &str) -> Result<()> {
    actas_core::export::write_transcript_md(&actas_core::Db::open_default()?, id)
}

/// Entry point for `actas`, `actas popover` and `actas --background`.
pub fn run(open: Option<Open>) -> Result<()> {
    // Hand the request to a running UI process.
    if let Ok(mut stream) = UnixStream::connect(ui_socket()) {
        if let Some(o) = open {
            writeln!(stream, "{}", o.as_str())?;
        }
        return Ok(());
    }
    let path = ui_socket();
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path)?;
    let (tx, rx) = gpui_kit::base::async_util::unbounded::<Open>();
    std::thread::Builder::new().name("actas-ui-sock".into()).spawn(move || {
        for stream in listener.incoming().flatten() {
            let mut line = String::new();
            if BufReader::new(stream).read_line(&mut line).is_ok()
                && let Some(o) = Open::parse(&line)
            {
                let _ = tx.send_blocking(o);
            }
        }
    })?;

    gpui_kit::application().with_assets(gpui_kit::assets::Assets).run(move |cx| {
        gpui_kit::init(cx);
        gpui_kit::component::Theme::change(gpui_kit::component::ThemeMode::Dark, None, cx);
        theme::init(cx);
        cx.set_quit_mode(QuitMode::Explicit);
        cx.set_global(Windows::default());
        cx.bind_keys([KeyBinding::new("escape", ClosePanel, Some("ActasPanel"))]);
        cx.on_window_closed(|cx, id| {
            let w = cx.global_mut::<Windows>();
            if w.main.is_some_and(|h| h.window_id() == id) {
                w.main = None;
            }
            if w.popover.is_some_and(|h| h.window_id() == id) {
                w.popover = None;
            }
        })
        .detach();
        if let Some(o) = open {
            handle(o, cx);
        }
        cx.spawn(async move |cx| {
            while let Ok(o) = rx.recv().await {
                cx.update(|cx| handle(o, cx));
            }
        })
        .detach();
    });
    let _ = std::fs::remove_file(path);
    Ok(())
}

pub fn handle(open: Open, cx: &mut App) {
    match open {
        Open::Main => open_main(cx),
        Open::Popover => toggle_popover(cx),
    }
}

pub fn open_main(cx: &mut App) {
    if let Some(h) = cx.global::<Windows>().main
        && h.update(cx, |_, window, _| window.activate_window()).is_ok()
    {
        return;
    }
    let glass = style::Palette::get(cx).glass;
    let options = WindowOptions {
        app_id: Some("actas".into()),
        window_bounds: Some(WindowBounds::Windowed(Bounds::centered(None, size(px(1180.), px(780.)), cx))),
        window_min_size: Some(size(px(820.), px(520.))),
        window_decorations: Some(WindowDecorations::Client),
        window_background: if glass {
            WindowBackgroundAppearance::Transparent
        } else {
            WindowBackgroundAppearance::Opaque
        },
        ..gpui_kit::component::TitleBar::window_options()
    };
    match gpui_kit::open_window(options, cx, |window, cx| cx.new(|cx| main_window::MainWindow::new(window, cx))) {
        Ok((handle, _)) => cx.global_mut::<Windows>().main = Some(handle),
        Err(e) => tracing::error!("no pude abrir la ventana: {e:#}"),
    }
}

pub fn close_popover(cx: &mut App) {
    if let Some(h) = cx.global_mut::<Windows>().popover.take() {
        let _ = h.update(cx, |_, window, _| window.remove_window());
    }
}

fn toggle_popover(cx: &mut App) {
    if cx.global::<Windows>().popover.is_some() {
        close_popover(cx);
        return;
    }
    let panel = size(px(380.), px(330.));
    // A layer surface anchored top-right, like the shell's own panels; the
    // compositor places it below the bar's exclusive zone.
    let layer = WindowOptions {
        app_id: Some("actas-popover".into()),
        titlebar: None,
        window_bounds: Some(WindowBounds::Windowed(Bounds { origin: point(px(0.), px(0.)), size: panel })),
        window_background: WindowBackgroundAppearance::Transparent,
        kind: WindowKind::LayerShell(layer_shell::LayerShellOptions {
            namespace: "actas-popover".into(),
            layer: layer_shell::Layer::Top,
            anchor: layer_shell::Anchor::TOP | layer_shell::Anchor::RIGHT,
            margin: Some((px(8.), px(8.), px(0.), px(0.))),
            keyboard_interactivity: layer_shell::KeyboardInteractivity::OnDemand,
            ..Default::default()
        }),
        ..Default::default()
    };
    // Opened without the component Root: its client-side border would add a
    // shadow inset around the layer surface.
    let handle = match cx.open_window(layer, |window, cx| cx.new(|cx| popover::Popover::new(window, cx))) {
        Ok(h) => Some(h.into()),
        Err(e) => {
            tracing::info!("sin layer-shell ({e:#}); abro una ventana normal");
            let options = WindowOptions {
                app_id: Some("actas-popover".into()),
                titlebar: None,
                window_bounds: Some(WindowBounds::Windowed(Bounds::centered(None, panel, cx))),
                is_resizable: false,
                ..Default::default()
            };
            gpui_kit::open_window(options, cx, |window, cx| cx.new(|cx| popover::Popover::new(window, cx)))
                .map(|(h, _)| h)
                .ok()
        }
    };
    cx.global_mut::<Windows>().popover = handle;
}
