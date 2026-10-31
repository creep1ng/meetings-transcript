//! Recording panel opened from the bar or the tray (`actas popover`).
//! A second invocation closes the open panel.

use std::sync::mpsc::Receiver;
use std::time::Duration;

use actas_core::fmt_ms;
use actas_core::ipc::{DaemonStatus, Request};
use gpui_kit::component::button::*;
use gpui_kit::component::*;
use gpui_kit::prelude::FluentBuilder;
use gpui_kit::*;

use super::style::{self, Palette};
use crate::model::{self, DaemonMsg};

pub struct Popover {
    status: Option<DaemonStatus>,
    offline: bool,
    error: Option<String>,
    rx: Receiver<DaemonMsg>,
    focus: FocusHandle,
}

impl Popover {
    pub fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let rx = model::follow_daemon();
        // Focused so Escape (bound in the "ActasPanel" context) closes it.
        let focus = cx.focus_handle();
        focus.focus(window, cx);
        cx.spawn(async move |this, cx| {
            loop {
                cx.background_executor().timer(Duration::from_millis(100)).await;
                if this.update(cx, |this, cx| this.drain(cx)).is_err() {
                    break;
                }
            }
        })
        .detach();
        Self { status: None, offline: false, error: None, rx, focus }
    }

    fn drain(&mut self, cx: &mut Context<Self>) {
        let mut changed = false;
        while let Ok(msg) = self.rx.try_recv() {
            changed = true;
            match msg {
                DaemonMsg::Status(s) => {
                    self.status = Some(s);
                    self.offline = false;
                }
                DaemonMsg::Offline => self.offline = true,
                DaemonMsg::Library(_) => {}
            }
        }
        if changed {
            cx.notify();
        }
    }

    fn send(&mut self, req: Request, cx: &mut Context<Self>) {
        let (tx, rx) = std::sync::mpsc::channel();
        model::request_async(req, move |r| {
            let _ = tx.send(r.err().map(|e| format!("{e:#}")));
        });
        cx.spawn(async move |this, cx| {
            let err = cx.background_executor().spawn(async move { rx.recv().ok().flatten() }).await;
            let _ = this.update(cx, |this, cx| {
                this.error = err;
                cx.notify();
            });
        })
        .detach();
    }

    fn meter(p: &Palette, level: f32) -> impl IntoElement {
        div()
            .h(px(6.))
            .w_full()
            .rounded(px(3.))
            .bg(p.track)
            .child(div().h_full().rounded(px(3.)).bg(p.ok).w(relative(level.clamp(0.0, 1.0).sqrt())))
    }

    fn row(p: &Palette, label: &'static str, value: impl IntoElement) -> impl IntoElement {
        h_flex()
            .gap_3()
            .items_center()
            .child(div().w(px(76.)).text_color(p.muted).child(label))
            .child(div().flex_1().child(value))
    }

    fn render_recording(&self, s: &DaemonStatus, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        let r = s.recording.as_ref().unwrap();
        let source = match (&r.source, &r.encoder) {
            (Some(src), Some(enc)) => format!("{src} · {enc}"),
            _ => "Solo audio".to_string(),
        };
        v_flex()
            .gap_3()
            .child(
                h_flex()
                    .gap_2()
                    .items_center()
                    .child(style::rec_dot(p, !r.paused))
                    .child(div().text_2xl().font_family(style::mono()).child(fmt_ms(r.elapsed_ms)))
                    .when(r.paused, |d| d.child(div().text_color(p.warning).child("en pausa")))
                    .child(div().flex_1())
                    .child(style::pill(p, "● 100 % local", p.ok)),
            )
            .child(div().text_color(p.muted).child(match &r.platform {
                Some(pl) => format!("{} · {pl}", r.title),
                None => r.title.clone(),
            }))
            .child(
                v_flex()
                    .gap_1p5()
                    .text_sm()
                    .child(Self::row(p, "Pantalla", div().child(source)))
                    .child(Self::row(p, "Micrófono", Self::meter(p, r.mic_level)))
                    .child(Self::row(p, "Sistema", Self::meter(p, r.system_level)))
                    .child(Self::row(
                        p,
                        "Disco",
                        div().child(format!(
                            "{} · quedan {}",
                            model::human_bytes(r.bytes_written),
                            model::human_bytes(r.disk_free)
                        )),
                    )),
            )
            .when(r.call_ended, |d| {
                d.child(div().text_sm().text_color(p.warning).child("La llamada parece haber terminado."))
            })
            .child(
                h_flex()
                    .gap_2()
                    .child(
                        Button::new("pause")
                            .label(if r.paused { "▶ Reanudar" } else { "⏸ Pausar" })
                            .on_click(cx.listener(|this, _, _, cx| this.send(Request::TogglePause, cx))),
                    )
                    .child(
                        Button::new("mark")
                            .label("⚑ Marca")
                            .on_click(cx.listener(|this, _, _, cx| this.send(Request::Mark { label: None }, cx))),
                    )
                    .child(div().flex_1())
                    .child(
                        Button::new("stop")
                            .danger()
                            .label("■ Detener")
                            .on_click(cx.listener(|this, _, _, cx| this.send(Request::Stop, cx))),
                    ),
            )
            .into_any_element()
    }

    fn render_idle(&self, s: &DaemonStatus, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        let mut col = v_flex().gap_3();
        if let Some(sg) = &s.suggestion {
            col = col
                .child(div().text_lg().child(match &sg.platform {
                    Some(pl) => format!("Llamada de {pl} detectada"),
                    None => "Llamada detectada".into(),
                }))
                .child(div().text_sm().text_color(p.muted).child(format!(
                    "{} usa el micrófono{}",
                    sg.app,
                    sg.title.as_ref().map(|t| format!(" · «{t}»")).unwrap_or_default()
                )))
                .child(
                    h_flex()
                        .gap_2()
                        .child(Button::new("rec").danger().label("● Grabar").on_click(
                            cx.listener(|this, _, _, cx| this.send(Request::AcceptSuggestion { video: true }, cx)),
                        ))
                        .child(Button::new("audio").label("Solo audio").on_click(
                            cx.listener(|this, _, _, cx| this.send(Request::AcceptSuggestion { video: false }, cx)),
                        ))
                        .child(
                            Button::new("dismiss")
                                .ghost()
                                .label("Ahora no")
                                .on_click(cx.listener(|this, _, _, cx| this.send(Request::DismissSuggestion, cx))),
                        ),
                );
        } else {
            col = col
                .child(
                    h_flex()
                        .gap_2()
                        .items_center()
                        .child(style::rec_dot(p, false))
                        .child(div().text_lg().child("Sin grabación"))
                        .child(div().flex_1())
                        .child(style::pill(p, "● 100 % local", p.ok)),
                )
                .child(
                    h_flex()
                        .gap_2()
                        .child(Button::new("rec").danger().label("● Grabar").on_click(
                            cx.listener(|this, _, _, cx| this.send(Request::Start { title: None, video: true }, cx)),
                        ))
                        .child(Button::new("audio").label("Solo audio").on_click(
                            cx.listener(|this, _, _, cx| this.send(Request::Start { title: None, video: false }, cx)),
                        )),
                );
        }
        if let Some(j) = &s.transcribing {
            col = col.child(
                v_flex()
                    .gap_1()
                    .text_sm()
                    .child(div().text_color(p.muted).child(format!("Transcribiendo «{}» · {}", j.title, j.stage)))
                    .child(style::progress(p, j.progress)),
            );
        } else if s.queued > 0 {
            col = col.child(div().text_sm().text_color(p.muted).child(format!("{} en cola", s.queued)));
        }
        if !s.missing_models.is_empty() {
            col = col.child(
                div()
                    .text_sm()
                    .text_color(p.warning)
                    .child("Faltan modelos de transcripción: ábrelos en Actas → Ajustes."),
            );
        }
        col.into_any_element()
    }
}

impl Render for Popover {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let p = Palette::get(cx);
        let body = match (&self.status, self.offline) {
            (_, true) | (None, _) => v_flex()
                .gap_2()
                .child(div().text_lg().child("Actas no está en marcha"))
                .child(div().text_sm().text_color(p.muted).child("Arranca el servicio: systemctl --user start actasd"))
                .into_any_element(),
            (Some(s), false) => {
                let s = s.clone();
                if s.recording.is_some() { self.render_recording(&s, &p, cx) } else { self.render_idle(&s, &p, cx) }
            }
        };
        v_flex()
            .key_context("ActasPanel")
            .track_focus(&self.focus)
            .on_action(|_: &crate::ui::ClosePanel, _, cx| crate::ui::close_popover(cx))
            .size_full()
            .p_4()
            .gap_3()
            .rounded(px(12.))
            .border_1()
            .border_color(p.border)
            .bg(p.bg)
            .text_color(p.fg)
            .child(body)
            .child(div().flex_1())
            .when_some(self.error.clone(), |d, e| d.child(div().text_sm().text_color(p.danger).child(e)))
            .child(
                h_flex()
                    .justify_between()
                    .child(
                        Button::new("close")
                            .ghost()
                            .small()
                            .label("Cerrar")
                            .on_click(|_, _, cx| crate::ui::close_popover(cx)),
                    )
                    .child(Button::new("open").ghost().small().label("Abrir Actas").on_click(|_, _, cx| {
                        crate::ui::close_popover(cx);
                        crate::ui::open_main(cx);
                    })),
            )
    }
}
