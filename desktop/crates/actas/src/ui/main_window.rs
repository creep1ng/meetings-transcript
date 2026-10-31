//! Main window: library sidebar, transcript with playback, speakers and voices.

use std::sync::mpsc::Receiver;
use std::time::Duration;

use actas_core::db::{Meeting, Status};
use actas_core::export::{Format, Transcript};
use actas_core::ipc::{DaemonStatus, Request};
use actas_core::{Config, Db, fmt_ms};
use gpui_kit::component::button::*;
use gpui_kit::component::dialog::*;
use gpui_kit::component::input::InputEvent;
use gpui_kit::component::input::*;
use gpui_kit::component::notification::Notification;
use gpui_kit::component::scroll::ScrollableElement;
use gpui_kit::component::tab::*;
use gpui_kit::component::*;
use gpui_kit::prelude::FluentBuilder;
use gpui_kit::*;

use super::settings::SettingsView;
use super::style::{self, Palette};
use crate::external;
use crate::model::{self, DaemonMsg, Detail};
use crate::player::Player;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Page {
    Library,
    Settings,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Pane {
    Transcript,
    Speakers,
    Marks,
}

pub struct MainWindow {
    db: Option<Db>,
    db_error: Option<String>,
    meetings: Vec<Meeting>,
    selected: Option<String>,
    detail: Detail,
    status: Option<DaemonStatus>,
    offline: bool,
    rx: Receiver<DaemonMsg>,
    page: Page,
    tab: Pane,
    search: Entity<InputState>,
    query: String,
    hits: Vec<actas_core::db::SearchHit>,
    edit: Entity<InputState>,
    player: Option<Player>,
    position_ms: i64,
    current_segment: Option<usize>,
    transcript_scroll: ScrollHandle,
    settings: Entity<SettingsView>,
    _subs: Vec<Subscription>,
}

impl MainWindow {
    pub fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let search = cx.new(|cx| InputState::new(window, cx).placeholder("Buscar en todas las transcripciones…"));
        let edit = cx.new(|cx| InputState::new(window, cx));
        let settings = cx.new(|cx| SettingsView::new(window, cx));
        let subs = vec![cx.subscribe_in(&search, window, |this, state, ev: &InputEvent, _, cx| {
            if let InputEvent::Change = ev {
                this.query = state.read(cx).value().to_string();
                this.run_search();
                cx.notify();
            }
        })];
        let (db, db_error) = match Db::open_default() {
            Ok(db) => (Some(db), None),
            Err(e) => (None, Some(format!("{e:#}"))),
        };
        let mut this = Self {
            db,
            db_error,
            meetings: Vec::new(),
            selected: None,
            detail: Detail::default(),
            status: None,
            offline: false,
            rx: model::follow_daemon(),
            page: Page::Library,
            tab: Pane::Transcript,
            search,
            query: String::new(),
            hits: Vec::new(),
            edit,
            player: None,
            position_ms: 0,
            current_segment: None,
            transcript_scroll: ScrollHandle::new(),
            settings,
            _subs: subs,
        };
        this.reload_library();
        if let Some(first) = this.meetings.first().map(|m| m.id.clone()) {
            this.select(first);
        }
        // Undocumented: open a given page, for screenshots and debugging.
        match std::env::var("ACTAS_START_PAGE").as_deref() {
            Ok("settings") => this.page = Page::Settings,
            Ok("speakers") => this.tab = Pane::Speakers,
            Ok("marks") => this.tab = Pane::Marks,
            _ => {}
        }
        cx.spawn(async move |this, cx| {
            loop {
                cx.background_executor().timer(Duration::from_millis(100)).await;
                if this.update(cx, |this, cx| this.tick(cx)).is_err() {
                    break;
                }
            }
        })
        .detach();
        this
    }

    // ---------- data ----------

    fn reload_library(&mut self) {
        if let Some(db) = &self.db {
            match db.meetings() {
                Ok(m) => self.meetings = m,
                Err(e) => tracing::warn!("leyendo la biblioteca: {e:#}"),
            }
        }
    }

    fn reload_detail(&mut self) {
        let (Some(db), Some(id)) = (&self.db, &self.selected) else {
            self.detail = Detail::default();
            return;
        };
        match Detail::load(db, id) {
            Ok(d) => self.detail = d,
            Err(e) => tracing::warn!("leyendo la reunión: {e:#}"),
        }
    }

    fn select(&mut self, id: String) {
        if self.selected.as_ref() != Some(&id) {
            self.player = None;
            self.position_ms = 0;
            self.current_segment = None;
        }
        self.selected = Some(id);
        self.page = Page::Library;
        self.reload_detail();
    }

    fn run_search(&mut self) {
        self.hits = match (&self.db, self.query.trim()) {
            (Some(db), q) if !q.is_empty() => db.search(q, 50).unwrap_or_default(),
            _ => Vec::new(),
        };
    }

    fn tick(&mut self, cx: &mut Context<Self>) {
        let mut changed = false;
        let mut library = false;
        while let Ok(msg) = self.rx.try_recv() {
            changed = true;
            match msg {
                DaemonMsg::Status(s) => {
                    self.status = Some(s);
                    self.offline = false;
                }
                DaemonMsg::Offline => self.offline = true,
                DaemonMsg::Library(_) => library = true,
            }
        }
        if library {
            self.reload_library();
            match self.selected.clone() {
                Some(id) if self.meetings.iter().any(|m| m.id == id) => self.reload_detail(),
                _ => {
                    // Nothing (valid) selected: show the newest meeting.
                    self.selected = None;
                    if let Some(first) = self.meetings.first().map(|m| m.id.clone()) {
                        self.select(first);
                    } else {
                        self.reload_detail();
                    }
                }
            }
        }
        if let Some(p) = &mut self.player {
            if !p.poll() {
                changed = true;
            }
            if p.is_playing() {
                self.position_ms = p.position_ms();
                let seg = self.detail.segment_at(self.position_ms);
                if seg != self.current_segment {
                    self.current_segment = seg;
                    if let Some(ix) = seg {
                        self.transcript_scroll.scroll_to_item(ix);
                    }
                }
                changed = true;
            }
        }
        if changed {
            cx.notify();
        }
    }

    fn meeting(&self) -> Option<&Meeting> {
        self.detail.meeting.as_ref()
    }

    // ---------- actions ----------

    fn toast(window: &mut Window, cx: &mut App, text: impl Into<SharedString>) {
        window.push_notification(Notification::new().message(text.into()), cx);
    }

    fn toast_err(window: &mut Window, cx: &mut App, e: anyhow::Error) {
        window.push_notification(Notification::error(format!("{e:#}")).autohide(false), cx);
    }

    fn send(&self, req: Request, window: &mut Window, cx: &mut Context<Self>) {
        let (tx, rx) = std::sync::mpsc::channel();
        model::request_async(req, move |r| {
            let _ = tx.send(r.err().map(|e| format!("{e:#}")));
        });
        cx.spawn_in(window, async move |_, cx| {
            let err = cx.background_executor().spawn(async move { rx.recv().ok().flatten() }).await;
            if let Some(e) = err {
                let _ = cx.update(|window, cx| {
                    window.push_notification(Notification::error(e), cx);
                });
            }
        })
        .detach();
    }

    fn ensure_player(&mut self) -> Option<&mut Player> {
        if self.player.is_none() {
            let m = self.meeting()?;
            match Player::open(&[&m.mic_wav(), &m.system_wav()]) {
                Ok(p) => self.player = Some(p),
                Err(e) => {
                    tracing::warn!("{e:#}");
                    return None;
                }
            }
        }
        self.player.as_mut()
    }

    fn play_from(&mut self, ms: i64, cx: &mut Context<Self>) {
        if let Some(p) = self.ensure_player() {
            p.seek_ms(ms);
            p.play();
        }
        self.position_ms = ms;
        cx.notify();
    }

    fn toggle_play(&mut self, cx: &mut Context<Self>) {
        let pos = self.position_ms;
        if let Some(p) = self.ensure_player() {
            if !p.is_playing() && pos > 0 {
                p.seek_ms(pos);
            }
            p.toggle();
        }
        cx.notify();
    }

    fn export_all(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let Some(m) = self.meeting().cloned() else { return };
        let t = Transcript {
            meeting: &m,
            speakers: &self.detail.speakers,
            segments: &self.detail.segments,
            marks: &self.detail.marks,
        };
        let dir = m.dir.join("exports");
        let result = (|| -> anyhow::Result<()> {
            std::fs::create_dir_all(&dir)?;
            for f in Format::ALL {
                std::fs::write(dir.join(format!("transcripcion.{}", f.extension())), t.render(f))?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                Self::toast(window, cx, "Exportado en Markdown, TXT, SRT y JSON");
                let _ = external::open_folder(&dir);
            }
            Err(e) => Self::toast_err(window, cx, e),
        }
    }

    fn confirm_codex(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let Some(m) = self.meeting().cloned() else { return };
        window.open_alert_dialog(cx, move |alert, _, _| {
            let m = m.clone();
            alert
                .confirm()
                .title("Resumir con Codex y guardar en Notion")
                .description(
                    "Se abrirá Codex en una terminal con esta transcripción. El texto saldrá de tu equipo \
                     hacia OpenAI y, si lo pides en la conversación, hacia Notion. La grabación y el audio \
                     no se envían.",
                )
                .ok_text("Abrir Codex")
                .cancel_text("Cancelar")
                .ok_variant(ButtonVariant::Warning)
                .on_ok(move |_, window, cx| {
                    let cfg = Config::load_or_default();
                    if let Err(e) = external::open_codex(&cfg.summary, &m) {
                        Self::toast_err(window, cx, e);
                    }
                    true
                })
                .on_cancel(|_, _, _| true)
        });
    }

    fn confirm_delete(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let Some(m) = self.meeting().cloned() else { return };
        let me = cx.entity().downgrade();
        window.open_alert_dialog(cx, move |alert, _, _| {
            let (me, m) = (me.clone(), m.clone());
            alert
                .confirm()
                .title(format!("¿Borrar «{}»?", m.title))
                .description("Se borran la grabación, el audio y la transcripción. No se puede deshacer.")
                .ok_text("Borrar")
                .cancel_text("Cancelar")
                .ok_variant(ButtonVariant::Danger)
                .on_ok(move |_, _, cx| {
                    let _ = me.update(cx, |this, cx| {
                        this.player = None;
                        if let Some(db) = &this.db
                            && let Ok(Some(dir)) = db.delete_meeting(&m.id)
                        {
                            let _ = std::fs::remove_dir_all(dir);
                        }
                        this.selected = None;
                        this.reload_library();
                        if let Some(first) = this.meetings.first().map(|m| m.id.clone()) {
                            this.select(first);
                        } else {
                            this.reload_detail();
                        }
                        cx.notify();
                    });
                    true
                })
                .on_cancel(|_, _, _| true)
        });
    }

    /// A dialog with one text field; `apply` gets the trimmed value.
    fn prompt(
        &mut self,
        title: impl Into<SharedString>,
        initial: &str,
        ok: &'static str,
        apply: impl Fn(&mut Self, String, &mut Window, &mut Context<Self>) + 'static,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        self.edit.update(cx, |s, cx| s.set_value(initial.to_string(), window, cx));
        let input = self.edit.clone();
        let me = cx.entity().downgrade();
        let title: SharedString = title.into();
        let apply = std::rc::Rc::new(apply);
        window.open_dialog(cx, move |dialog, _, _| {
            let (input, me, apply) = (input.clone(), me.clone(), apply.clone());
            dialog
                .title(title.clone())
                .w(px(420.))
                .child(Input::new(&input))
                .footer(
                    DialogFooter::new()
                        .child(DialogClose::new().trigger(|b| b.label("Cancelar")))
                        .child(DialogAction::new().child(Button::new("ok").primary().label(ok))),
                )
                .on_ok(move |_, window, cx| {
                    let value = input.read(cx).value().trim().to_string();
                    let apply = apply.clone();
                    let _ = me.update(cx, |this, cx| {
                        apply(this, value, window, cx);
                        cx.notify();
                    });
                    true
                })
        });
        self.edit.update(cx, |s, cx| s.focus(window, cx));
    }

    fn rename_meeting(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let Some(m) = self.meeting().cloned() else { return };
        self.prompt(
            "Renombrar reunión",
            &m.title,
            "Guardar",
            move |this, value, window, cx| {
                if value.is_empty() {
                    return;
                }
                let result = this.db.as_ref().map(|db| db.rename_meeting(&m.id, &value));
                if let Some(Err(e)) = result {
                    Self::toast_err(window, cx, e);
                }
                this.reload_library();
                this.reload_detail();
                if this.meeting().is_some_and(|m| m.status == Status::Done) {
                    let _ = crate::ui::rewrite_markdown(&m.id);
                }
            },
            window,
            cx,
        );
    }

    fn rename_speaker(&mut self, speaker_id: i64, current: String, window: &mut Window, cx: &mut Context<Self>) {
        self.prompt(
            "Nombre del hablante (solo en esta reunión)",
            &current,
            "Guardar",
            move |this, value, window, cx| {
                if let Some(Err(e)) = this.db.as_ref().map(|db| db.rename_speaker(speaker_id, Some(&value))) {
                    Self::toast_err(window, cx, e);
                }
                this.reload_detail();
                if let Some(id) = this.selected.clone() {
                    let _ = crate::ui::rewrite_markdown(&id);
                }
            },
            window,
            cx,
        );
    }

    fn save_voice(&mut self, speaker_id: i64, current: String, window: &mut Window, cx: &mut Context<Self>) {
        self.prompt(
            "Guardar voz: se reconocerá en próximas reuniones",
            &current,
            "Guardar voz",
            move |this, value, window, cx| {
                let result = Db::open_default().and_then(|mut db| db.save_voice_from_speaker(speaker_id, &value));
                match result {
                    Ok(_) => Self::toast(window, cx, format!("Voz «{value}» guardada")),
                    Err(e) => Self::toast_err(window, cx, e),
                }
                this.reload_detail();
                if let Some(id) = this.selected.clone() {
                    let _ = crate::ui::rewrite_markdown(&id);
                }
            },
            window,
            cx,
        );
    }

    // ---------- render ----------

    /// "Actas": back to the library, clearing the search.
    fn go_home(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.page = Page::Library;
        self.query.clear();
        self.hits.clear();
        self.search.update(cx, |s, cx| s.set_value("", window, cx));
        if self.selected.is_none()
            && let Some(first) = self.meetings.first().map(|m| m.id.clone())
        {
            self.select(first);
        }
        cx.notify();
    }

    /// Record control: two buttons when idle, a live chip while recording.
    fn render_record_control(&self, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        match self.status.as_ref().and_then(|s| s.recording.clone()) {
            None => h_flex()
                .gap_1()
                .child(
                    Button::new("rec")
                        .danger()
                        .small()
                        .label("● Grabar")
                        .tooltip("Grabar pantalla y audio")
                        .disabled(self.offline)
                        .on_click(cx.listener(|this, _, window, cx| {
                            this.send(Request::Start { title: None, video: true }, window, cx)
                        })),
                )
                .child(
                    Button::new("rec-audio")
                        .ghost()
                        .small()
                        .icon(IconName::Mic)
                        .tooltip("Grabar solo audio")
                        .disabled(self.offline)
                        .on_click(cx.listener(|this, _, window, cx| {
                            this.send(Request::Start { title: None, video: false }, window, cx)
                        })),
                )
                .into_any_element(),
            Some(r) => h_flex()
                .gap_1()
                .pl_2()
                .rounded(px(8.))
                .border_1()
                .border_color(p.rec.opacity(0.6))
                .bg(p.rec.opacity(0.12))
                .child(style::rec_dot(p, !r.paused))
                .child(div().text_sm().font_family(style::mono()).text_color(p.rec).child(if r.paused {
                    format!("En pausa · {}", fmt_ms(r.elapsed_ms))
                } else {
                    fmt_ms(r.elapsed_ms)
                }))
                .child(
                    Button::new("rec-pause")
                        .ghost()
                        .xsmall()
                        .icon(if r.paused { IconName::Play } else { IconName::Pause })
                        .tooltip(if r.paused { "Reanudar" } else { "Pausar" })
                        .on_click(cx.listener(|this, _, window, cx| this.send(Request::TogglePause, window, cx))),
                )
                .child(
                    Button::new("rec-stop")
                        .danger()
                        .xsmall()
                        .label("■ Detener")
                        .on_click(cx.listener(|this, _, window, cx| this.send(Request::Stop, window, cx))),
                )
                .into_any_element(),
        }
    }

    fn render_titlebar(&self, p: &Palette, cx: &mut Context<Self>) -> impl IntoElement {
        TitleBar::new().child(
            h_flex()
                .w_full()
                .pr_2()
                .gap_3()
                .child(
                    div()
                        .id("home")
                        .px_2()
                        .py_0p5()
                        .rounded(px(6.))
                        .font_semibold()
                        .cursor_pointer()
                        .hover(|s| s.bg(p.track))
                        .child("Actas")
                        .on_click(cx.listener(|this, _, window, cx| this.go_home(window, cx))),
                )
                .child(div().flex_1().max_w(px(520.)).child(
                    Input::new(&self.search).small().cleanable(true).prefix(Icon::new(IconName::Search).small()),
                ))
                .child(div().flex_1())
                .child(self.render_record_control(p, cx))
                .child(
                    Button::new("settings")
                        .ghost()
                        .small()
                        .icon(IconName::Settings)
                        .tooltip("Ajustes")
                        .toggled(self.page == Page::Settings)
                        .on_click(cx.listener(|this, _, _, cx| {
                            this.page = if this.page == Page::Settings { Page::Library } else { Page::Settings };
                            cx.notify();
                        })),
                ),
        )
    }

    /// Bottom bar: daemon, models, queue, and the privacy promise.
    fn render_statusbar(&self, p: &Palette) -> impl IntoElement {
        let dot = |c: Hsla| div().size(px(7.)).rounded_full().bg(c);
        let mut left = h_flex().gap_2().items_center();
        match (&self.status, self.offline) {
            (Some(s), false) => {
                left = left.child(dot(p.ok)).child("Servicio activo");
                if !s.missing_models.is_empty() {
                    left = left.child("·").child(div().text_color(p.warning).child("Faltan modelos (Ajustes)"));
                }
                if let Some(j) = &s.transcribing {
                    left = left
                        .child("·")
                        .child(format!("Transcribiendo «{}» {:.0} %", j.title, j.progress * 100.0))
                        .child(div().w(px(80.)).child(style::progress(p, j.progress)));
                } else if s.queued > 0 {
                    left = left.child("·").child(format!("{} en cola", s.queued));
                }
            }
            _ => {
                left = left
                    .child(dot(p.warning))
                    .child("Servicio parado")
                    .child(div().text_color(p.muted).child("— systemctl --user start actasd"));
            }
        }
        h_flex()
            .h(px(26.))
            .px_3()
            .gap_3()
            .border_t_1()
            .border_color(p.border)
            .text_xs()
            .text_color(p.muted)
            .child(left)
            .child(div().flex_1())
            .child(div().id("local").child("Grabación y transcripción en este equipo · sin red").tooltip(
                |window, cx| {
                    gpui_kit::component::tooltip::Tooltip::new(
                        "Solo «Resumen → Notion» envía la transcripción fuera, y siempre pide confirmación.",
                    )
                    .build(window, cx)
                },
            ))
    }

    fn render_sidebar(&self, p: &Palette, cx: &mut Context<Self>) -> impl IntoElement {
        let mut col = v_flex().w(px(250.)).h_full().border_r_1().border_color(p.border).p_2().gap_0p5();
        let group_label =
            |text: &str| div().px_2().pt_3().pb_1().text_xs().text_color(p.muted).child(text.to_uppercase());
        if !self.query.trim().is_empty() {
            col = col.child(group_label(&format!("{} resultados", self.hits.len())));
            for (i, hit) in self.hits.iter().enumerate() {
                let title =
                    self.meetings.iter().find(|m| m.id == hit.meeting_id).map(|m| m.title.clone()).unwrap_or_default();
                let (id, at) = (hit.meeting_id.clone(), hit.start_ms);
                col = col.child(
                    v_flex()
                        .id(("hit", i))
                        .px_2()
                        .py_1p5()
                        .rounded(px(6.))
                        .cursor_pointer()
                        .hover(|s| s.bg(p.track))
                        .child(div().text_xs().text_color(p.muted).child(format!("{title} · {}", fmt_ms(at))))
                        .child(div().text_sm().child(hit.snippet.clone()))
                        .on_click(cx.listener(move |this, _, _, cx| {
                            this.select(id.clone());
                            this.tab = Pane::Transcript;
                            if let Some(ix) = this.detail.segments.iter().position(|s| s.start_ms == at) {
                                this.current_segment = Some(ix);
                                this.transcript_scroll.scroll_to_item(ix);
                            }
                            this.position_ms = at;
                            cx.notify();
                        })),
                );
            }
            return col.overflow_y_scrollbar();
        }
        if self.meetings.is_empty() {
            col =
                col.child(div().p_3().text_sm().text_color(p.muted).child(
                    "Aún no hay grabaciones. Cuando una llamada use el micrófono, Actas te propondrá grabarla.",
                ));
        }
        let mut last_group = "";
        for m in &self.meetings {
            let group = if m.status == Status::Queued { "En cola" } else { model::day_group(m) };
            if group != last_group {
                col = col.child(group_label(group));
                last_group = group;
            }
            let selected = self.selected.as_ref() == Some(&m.id);
            let state = match m.status {
                Status::Recording => div().text_color(p.rec).child("● grabando"),
                Status::Queued => div().child("⏳ en cola"),
                Status::Transcribing => div().text_color(p.info).child(format!("{:.0} %", m.progress * 100.0)),
                Status::Failed => div().text_color(p.danger).child("error"),
                Status::Done => div().child("✓"),
            };
            let id = m.id.clone();
            col = col.child(
                v_flex()
                    .id(SharedString::from(format!("m-{}", m.id)))
                    .px_2()
                    .py_1p5()
                    .rounded(px(6.))
                    .cursor_pointer()
                    .when(selected, |d| d.bg(p.accent))
                    .when(!selected, |d| d.hover(|s| s.bg(p.track)))
                    .child(div().text_sm().font_semibold().truncate().child(m.title.clone()))
                    .child(
                        h_flex()
                            .gap_1p5()
                            .text_xs()
                            .text_color(p.muted)
                            .when_some(m.platform.clone(), |d, pl| d.child(pl))
                            .child(m.started_local().format("%d/%m %H:%M").to_string())
                            .child("·")
                            .child(fmt_ms(m.duration_ms))
                            .child("·")
                            .child(state),
                    )
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.select(id.clone());
                        cx.notify();
                    })),
            );
        }
        col.overflow_y_scrollbar()
    }

    fn render_timeline(&self, p: &Palette) -> Div {
        let Some(m) = self.meeting() else { return div() };
        let total = m.duration_ms.max(self.detail.segments.last().map(|s| s.end_ms).unwrap_or(0)).max(1) as f32;
        let mut lanes = div().relative().h(px(46.)).w_full().rounded(px(6.)).bg(p.track).overflow_hidden();
        for (color, start, end) in self.detail.turns() {
            let top = if color == 0 { 6. } else { 26. };
            lanes = lanes.child(
                div()
                    .absolute()
                    .top(px(top))
                    .h(px(14.))
                    .left(relative(start as f32 / total))
                    .w(relative(((end - start) as f32 / total).max(0.003)))
                    .rounded(px(3.))
                    .bg(p.speaker(color).opacity(0.85)),
            );
        }
        for mk in &self.detail.marks {
            lanes = lanes.child(
                div()
                    .absolute()
                    .top_0()
                    .left(relative(mk.at_ms as f32 / total))
                    .text_xs()
                    .text_color(p.warning)
                    .child("⚑"),
            );
        }
        lanes.child(
            div().absolute().top_0().bottom_0().w(px(2.)).bg(p.fg).left(relative(self.position_ms as f32 / total)),
        )
    }

    fn render_status_banner(&self, m: &Meeting, p: &Palette, cx: &mut Context<Self>) -> Option<AnyElement> {
        let missing = self.status.as_ref().is_some_and(|s| !s.missing_models.is_empty());
        match m.status {
            Status::Recording => Some(div().text_sm().text_color(p.rec).child("● Grabando…").into_any_element()),
            Status::Queued => Some(
                div()
                    .text_sm()
                    .text_color(p.muted)
                    .child(if missing {
                        "En cola: faltan los modelos de transcripción (Ajustes → Transcripción)."
                    } else {
                        "En cola para transcribir."
                    })
                    .into_any_element(),
            ),
            Status::Transcribing => Some(
                v_flex()
                    .gap_1()
                    .child(
                        h_flex()
                            .text_sm()
                            .text_color(p.muted)
                            .child(div().flex_1().child(m.stage.clone().unwrap_or_else(|| "Transcribiendo…".into())))
                            .child(format!("{:.0} %", m.progress * 100.0)),
                    )
                    .child(style::progress(p, m.progress))
                    .into_any_element(),
            ),
            Status::Failed => Some(
                h_flex()
                    .gap_3()
                    .text_sm()
                    .child(
                        div()
                            .flex_1()
                            .text_color(p.danger)
                            .child(format!("La transcripción falló: {}", m.error.clone().unwrap_or_default())),
                    )
                    .child(Button::new("retry").small().label("Reintentar").on_click({
                        let id = m.id.clone();
                        cx.listener(move |this, _, window, cx| {
                            this.send(Request::Transcribe { meeting_id: id.clone() }, window, cx)
                        })
                    }))
                    .into_any_element(),
            ),
            Status::Done => None,
        }
    }

    fn render_transcript(&self, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        if self.detail.segments.is_empty() {
            return div().p_4().text_color(p.muted).child("Todavía no hay transcripción.").into_any_element();
        }
        let mono = style::mono();
        let mut list =
            div().id("transcript").flex_1().min_w_0().overflow_y_scroll().track_scroll(&self.transcript_scroll);
        let mut last_speaker = None;
        for (ix, seg) in self.detail.segments.iter().enumerate() {
            let sp = self.detail.speaker(seg.speaker_id);
            let header = last_speaker != Some(seg.speaker_id);
            last_speaker = Some(seg.speaker_id);
            let current = self.current_segment == Some(ix);
            let start = seg.start_ms;
            list = list.child(
                h_flex()
                    .id(("seg", ix))
                    .items_start()
                    .gap_3()
                    .px_2()
                    .py_1()
                    .rounded(px(6.))
                    .cursor_pointer()
                    .when(current, |d| d.bg(p.accent))
                    .when(!current, |d| d.hover(|s| s.bg(p.card)))
                    .child(
                        div()
                            .w(px(52.))
                            .pt(px(2.))
                            .text_xs()
                            .font_family(mono.clone())
                            .text_color(p.muted)
                            .child(fmt_ms(seg.start_ms)),
                    )
                    .child(
                        v_flex()
                            .flex_1()
                            .min_w_0()
                            .when(header, |d| {
                                d.child(
                                    div()
                                        .text_xs()
                                        .font_semibold()
                                        .text_color(p.speaker(sp.map(|s| s.color).unwrap_or(7)))
                                        .child(sp.map(|s| s.display_name().to_string()).unwrap_or_else(|| "?".into())),
                                )
                            })
                            .child(div().w_full().text_sm().child(seg.text.clone())),
                    )
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.current_segment = Some(ix);
                        this.play_from(start, cx);
                    })),
            );
        }
        list.into_any_element()
    }

    fn render_speakers(&self, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        let mut col = v_flex().gap_2().p_1();
        for sp in &self.detail.speakers {
            let words: usize = self
                .detail
                .segments
                .iter()
                .filter(|s| s.speaker_id == Some(sp.id))
                .map(|s| s.text.split_whitespace().count())
                .sum();
            let info = match (sp.voice_id, sp.similarity) {
                (Some(_), Some(sim)) if sim < 0.999 => format!("voz reconocida · {sim:.2}"),
                (Some(_), _) => "voz guardada".to_string(),
                _ if sp.track == "mic" => "tu micrófono".to_string(),
                _ if sp.has_embedding => "voz desconocida".to_string(),
                _ => "habló muy poco para reconocerla".to_string(),
            };
            let (id, name) = (sp.id, sp.display_name().to_string());
            let can_save = sp.has_embedding;
            col = col.child(
                h_flex()
                    .gap_3()
                    .p_2()
                    .rounded(px(8.))
                    .border_1()
                    .border_color(p.border)
                    .child(div().size(px(10.)).rounded_full().bg(p.speaker(sp.color)))
                    .child(
                        v_flex()
                            .flex_1()
                            .child(div().font_semibold().child(name.clone()))
                            .child(div().text_xs().text_color(p.muted).child(format!("{info} · {words} palabras"))),
                    )
                    .child(Button::new(("rename", id as usize)).small().ghost().label("Renombrar").on_click({
                        let name = name.clone();
                        cx.listener(move |this, _, window, cx| this.rename_speaker(id, name.clone(), window, cx))
                    }))
                    .when(can_save, |d| {
                        d.child(Button::new(("voice", id as usize)).small().label("Guardar voz").on_click({
                            let name = if name.starts_with("Hablante") { String::new() } else { name.clone() };
                            cx.listener(move |this, _, window, cx| this.save_voice(id, name.clone(), window, cx))
                        }))
                    }),
            );
        }
        col.child(
            div()
                .text_xs()
                .text_color(p.muted)
                .child("«Guardar voz» guarda la huella de ese hablante (solo en este equipo) para reconocerlo en las siguientes reuniones."),
        )
        .into_any_element()
    }

    fn render_marks(&self, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        if self.detail.marks.is_empty() {
            return div()
                .p_4()
                .text_color(p.muted)
                .child("Sin marcas. Añádelas durante la grabación con «⚑ Marca».")
                .into_any_element();
        }
        let mut col = v_flex().gap_1();
        for (i, mk) in self.detail.marks.iter().enumerate() {
            let at = mk.at_ms;
            col = col.child(
                h_flex()
                    .id(("mark", i))
                    .gap_3()
                    .px_2()
                    .py_1()
                    .rounded(px(6.))
                    .cursor_pointer()
                    .hover(|s| s.bg(p.card))
                    .child(div().text_color(p.warning).child("⚑"))
                    .child(div().w(px(52.)).text_xs().font_family(style::mono()).text_color(p.muted).child(fmt_ms(at)))
                    .child(div().text_sm().child(mk.label.clone()))
                    .on_click(cx.listener(move |this, _, _, cx| this.play_from(at, cx))),
            );
        }
        col.into_any_element()
    }

    fn render_detail(&self, p: &Palette, cx: &mut Context<Self>) -> AnyElement {
        let Some(m) = self.meeting().cloned() else {
            return div()
                .flex_1()
                .p_6()
                .text_color(p.muted)
                .child(if self.db_error.is_some() {
                    format!("No se pudo abrir la biblioteca: {}", self.db_error.clone().unwrap_or_default())
                } else {
                    "Selecciona una reunión.".into()
                })
                .into_any_element();
        };
        let playing = self.player.as_ref().is_some_and(Player::is_playing);
        let done = m.status == Status::Done;
        let busy = matches!(m.status, Status::Recording | Status::Transcribing);
        let mut legend = h_flex().gap_3().text_xs();
        for sp in &self.detail.speakers {
            legend = legend.child(div().text_color(p.speaker(sp.color)).child(format!("■ {}", sp.display_name())));
        }
        let tab_index = match self.tab {
            Pane::Transcript => 0,
            Pane::Speakers => 1,
            Pane::Marks => 2,
        };
        v_flex()
            .flex_1()
            .min_w_0()
            .h_full()
            .p_4()
            .gap_3()
            .child(
                h_flex()
                    .gap_2()
                    .child(
                        div()
                            .id("title")
                            .flex_1()
                            .text_xl()
                            .font_semibold()
                            .truncate()
                            .cursor_pointer()
                            .child(m.title.clone())
                            .on_click(cx.listener(|this, _, window, cx| this.rename_meeting(window, cx))),
                    )
                    .child(
                        Button::new("codex")
                            .small()
                            .label("✦ Resumen → Notion")
                            .disabled(!done)
                            .tooltip("Abre Codex con la transcripción (sale del equipo)")
                            .on_click(cx.listener(|this, _, window, cx| this.confirm_codex(window, cx))),
                    )
                    .child(
                        Button::new("export")
                            .small()
                            .label("⤓ Exportar")
                            .disabled(!done)
                            .on_click(cx.listener(|this, _, window, cx| this.export_all(window, cx))),
                    )
                    .child(
                        Button::new("video")
                            .small()
                            .ghost()
                            .label("Vídeo")
                            .disabled(busy || !m.video_path().exists())
                            .tooltip("Abrir la grabación en el reproductor")
                            .on_click(cx.listener(|this, _, window, cx| {
                                if let Some(m) = this.meeting()
                                    && let Err(e) = external::open_video(&m.video_path(), this.position_ms)
                                {
                                    Self::toast_err(window, cx, e);
                                }
                            })),
                    )
                    .child(
                        Button::new("folder")
                            .small()
                            .ghost()
                            .icon(IconName::FolderOpen)
                            .tooltip("Abrir carpeta")
                            .on_click(cx.listener(|this, _, _, _| {
                                if let Some(m) = this.meeting() {
                                    let _ = external::open_folder(&m.dir);
                                }
                            })),
                    )
                    .child(
                        Button::new("retranscribe")
                            .small()
                            .ghost()
                            .icon(IconName::RefreshCw)
                            .tooltip("Volver a transcribir (p. ej. tras guardar voces)")
                            .disabled(busy)
                            .on_click({
                                let id = m.id.clone();
                                cx.listener(move |this, _, window, cx| {
                                    this.send(Request::Transcribe { meeting_id: id.clone() }, window, cx)
                                })
                            }),
                    )
                    .child(
                        Button::new("delete")
                            .small()
                            .ghost()
                            .icon(IconName::Delete)
                            .tooltip("Borrar")
                            .disabled(busy)
                            .on_click(cx.listener(|this, _, window, cx| this.confirm_delete(window, cx))),
                    ),
            )
            .child(
                h_flex()
                    .gap_2()
                    .flex_wrap()
                    .text_sm()
                    .text_color(p.muted)
                    .child(format!("{} · {}", m.started_local().format("%d/%m/%Y %H:%M"), fmt_ms(m.duration_ms)))
                    .when_some(m.platform.clone(), |d, pl| d.child(style::pill(p, pl, p.muted)))
                    .when_some(m.model.clone(), |d, md| d.child(style::pill(p, md, p.muted)))
                    .child(style::pill(p, format!("{} hablantes", self.detail.speakers.len()), p.muted))
                    .when(!m.has_video, |d| d.child(style::pill(p, "solo audio", p.muted))),
            )
            .children(self.render_status_banner(&m, p, cx))
            .child(self.render_timeline(p))
            .child(
                h_flex()
                    .gap_3()
                    .text_sm()
                    .child(
                        Button::new("play")
                            .small()
                            .ghost()
                            .icon(if playing { IconName::Pause } else { IconName::Play })
                            .disabled(busy)
                            .on_click(cx.listener(|this, _, _, cx| this.toggle_play(cx))),
                    )
                    .child(div().font_family(style::mono()).text_color(p.muted).child(format!(
                        "{} / {}",
                        fmt_ms(self.position_ms),
                        fmt_ms(m.duration_ms)
                    )))
                    .child(div().flex_1())
                    .child(legend),
            )
            .child(
                TabBar::new("tabs")
                    .underline()
                    .selected_index(tab_index)
                    .children([
                        Tab::new().label(format!("Transcripción ({})", self.detail.segments.len())),
                        Tab::new().label("Hablantes"),
                        Tab::new().label(format!("Marcas ({})", self.detail.marks.len())),
                    ])
                    .on_click(cx.listener(|this, ix: &usize, _, cx| {
                        this.tab = match ix {
                            1 => Pane::Speakers,
                            2 => Pane::Marks,
                            _ => Pane::Transcript,
                        };
                        cx.notify();
                    })),
            )
            .child(div().flex_1().min_h_0().flex().flex_col().child(match self.tab {
                Pane::Transcript => self.render_transcript(p, cx),
                Pane::Speakers => {
                    div().flex_1().overflow_y_scrollbar().child(self.render_speakers(p, cx)).into_any_element()
                }
                Pane::Marks => div().flex_1().overflow_y_scrollbar().child(self.render_marks(p, cx)).into_any_element(),
            }))
            .into_any_element()
    }
}

impl Render for MainWindow {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let p = Palette::get(cx);
        let body = match self.page {
            Page::Library => h_flex()
                .flex_1()
                .min_h_0()
                .items_start()
                .child(self.render_sidebar(&p, cx))
                .child(self.render_detail(&p, cx))
                .into_any_element(),
            Page::Settings => div().flex_1().min_h_0().flex().child(self.settings.clone()).into_any_element(),
        };
        v_flex()
            .size_full()
            .bg(p.bg)
            .text_color(p.fg)
            .child(self.render_titlebar(&p, cx))
            .child(body)
            .child(self.render_statusbar(&p))
    }
}
