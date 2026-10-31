//! StatusNotifierItem icon (works in Noctalia's tray and any SNI host).
//! Left click opens the popover, middle click starts/stops, right click the menu.

use actas_core::fmt_ms;
use actas_core::ipc::DaemonStatus;

use crate::daemon::Daemon;

pub struct Tray {
    pub daemon: Daemon,
    pub status: DaemonStatus,
}

/// A filled circle (ARGB32, network byte order) for the given state.
fn dot(argb: [u8; 4], ring_only: bool) -> ksni::Icon {
    let size = 32i32;
    let mut data = Vec::with_capacity((size * size * 4) as usize);
    let c = (size as f32 - 1.0) / 2.0;
    for y in 0..size {
        for x in 0..size {
            let d = ((x as f32 - c).powi(2) + (y as f32 - c).powi(2)).sqrt();
            let outer = (12.5 - d).clamp(0.0, 1.0);
            let inner = if ring_only { 1.0 - (9.5 - d).clamp(0.0, 1.0) } else { 1.0 };
            let a = (outer * inner * argb[0] as f32) as u8;
            data.extend_from_slice(&[a, argb[1], argb[2], argb[3]]);
        }
    }
    ksni::Icon { width: size, height: size, data }
}

pub fn launch(args: &[&str]) {
    let exe = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("actas")))
        .filter(|p| p.exists())
        .unwrap_or_else(|| "actas".into());
    match std::process::Command::new(&exe).args(args).spawn() {
        Ok(mut child) => {
            std::thread::spawn(move || {
                let _ = child.wait();
            });
        }
        Err(e) => tracing::warn!("no pude abrir {}: {e}", exe.display()),
    }
}

impl ksni::Tray for Tray {
    fn id(&self) -> String {
        "actas".into()
    }

    fn title(&self) -> String {
        "Actas".into()
    }

    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        let icon = match self.status.state_name() {
            "recording" => dot([255, 0xe5, 0x48, 0x4d], false),
            "paused" => dot([255, 0xe5, 0x48, 0x4d], true),
            "suggest" => dot([255, 0xf5, 0xa5, 0x24], false),
            _ => dot([230, 0x9a, 0x9a, 0x9a], true),
        };
        vec![icon]
    }

    fn status(&self) -> ksni::Status {
        match self.status.state_name() {
            "recording" | "suggest" => ksni::Status::NeedsAttention,
            _ => ksni::Status::Active,
        }
    }

    fn tool_tip(&self) -> ksni::ToolTip {
        let s = &self.status;
        let description = match (&s.recording, &s.suggestion, &s.transcribing) {
            (Some(r), ..) => {
                format!("{} «{}» · {}", if r.paused { "En pausa" } else { "Grabando" }, r.title, fmt_ms(r.elapsed_ms))
            }
            (None, Some(sg), _) => format!("Llamada detectada en {} — clic central para grabar", sg.app),
            (None, None, Some(j)) => format!("Transcribiendo «{}» · {:.0} %", j.title, j.progress * 100.0),
            _ => "En espera".into(),
        };
        ksni::ToolTip { title: "Actas".into(), description, ..Default::default() }
    }

    fn activate(&mut self, _x: i32, _y: i32) {
        launch(&["popover"]);
    }

    fn secondary_activate(&mut self, _x: i32, _y: i32) {
        self.daemon.spawn(|d| async move {
            if let Err(e) = d.toggle().await {
                tracing::warn!("{e:#}");
            }
        });
    }

    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        use ksni::menu::*;
        let item = |label: &str, f: fn(&mut Self)| -> ksni::MenuItem<Self> {
            StandardItem { label: label.into(), activate: Box::new(f), ..Default::default() }.into()
        };
        let mut items: Vec<ksni::MenuItem<Self>> = Vec::new();
        match &self.status.recording {
            Some(r) => {
                items.push(
                    StandardItem {
                        label: format!("{} · {}", r.title, fmt_ms(r.elapsed_ms)),
                        enabled: false,
                        ..Default::default()
                    }
                    .into(),
                );
                items.push(item(if r.paused { "Reanudar" } else { "Pausar" }, |t| {
                    if let Err(e) = t.daemon.toggle_pause() {
                        tracing::warn!("{e:#}");
                    }
                }));
                items.push(item("Añadir marca", |t| {
                    if let Err(e) = t.daemon.mark(None) {
                        tracing::warn!("{e:#}");
                    }
                }));
                items.push(item("Detener", |t| {
                    t.daemon.spawn(|d| async move {
                        let _ = d.stop().await;
                    })
                }));
            }
            None => {
                items.push(item("Grabar", |t| {
                    t.daemon.spawn(|d| async move {
                        let _ = d.start(None, true).await;
                    })
                }));
                items.push(item("Grabar solo audio", |t| {
                    t.daemon.spawn(|d| async move {
                        let _ = d.start(None, false).await;
                    })
                }));
            }
        }
        if let Some(j) = &self.status.transcribing {
            items.push(
                StandardItem {
                    label: format!("Transcribiendo «{}» · {:.0} %", j.title, j.progress * 100.0),
                    enabled: false,
                    ..Default::default()
                }
                .into(),
            );
        }
        items.push(MenuItem::Separator);
        items.push(item("Abrir Actas", |_| launch(&[])));
        items.push(item("Elegir otra ventana para grabar", |t| t.daemon.forget_screen_source()));
        items.push(MenuItem::Separator);
        items.push(item("Salir", |t| {
            t.daemon.spawn(|d| async move {
                d.shutdown().await;
                std::process::exit(0);
            })
        }));
        items
    }
}
