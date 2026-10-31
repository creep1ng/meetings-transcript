//! Settings page: edits a copy of `config.toml`, saves it and asks the
//! daemon to reload. Also manages saved voices and model downloads.

use std::sync::mpsc::Receiver;
use std::time::Duration;

use actas_core::Db;
use actas_core::config::{Config, Device, OnDetect, VideoEncoder};
use actas_core::db::Voice;
use actas_core::ipc::Request;
use gpui_kit::component::button::*;
use gpui_kit::component::dialog::*;
use gpui_kit::component::input::InputEvent;
use gpui_kit::component::input::*;
use gpui_kit::component::notification::Notification;
use gpui_kit::component::scroll::ScrollableElement;
use gpui_kit::component::select::*;
use gpui_kit::component::switch::Switch;
use gpui_kit::component::*;
use gpui_kit::prelude::FluentBuilder;
use gpui_kit::*;

use super::style::{self, Palette};
use crate::cli;

type Choice = SelectState<SearchableVec<SharedString>>;

const ON_DETECT: [(OnDetect, &str); 3] = [
    (OnDetect::Suggest, "Sugerir grabación"),
    (OnDetect::Record, "Grabar siempre"),
    (OnDetect::Ignore, "No hacer nada"),
];
const FPS: [u32; 4] = [10, 15, 24, 30];
const DEVICES: [(Device, &str); 2] = [(Device::Cuda, "GPU (CUDA)"), (Device::Cpu, "CPU")];
const THRESHOLDS: [f32; 6] = [0.65, 0.70, 0.75, 0.80, 0.85, 0.90];

enum DownloadMsg {
    Progress(String, f32),
    Done(Result<(), String>),
}

pub struct SettingsView {
    cfg: Config,
    on_detect: Entity<Choice>,
    encoder: Entity<Choice>,
    fps: Entity<Choice>,
    device: Entity<Choice>,
    threshold: Entity<Choice>,
    apps: Entity<InputState>,
    mic_device: Entity<InputState>,
    monitor_device: Entity<InputState>,
    terminal: Entity<InputState>,
    codex: Entity<InputState>,
    prompt: Entity<InputState>,
    recordings_dir: Entity<InputState>,
    edit: Entity<InputState>,
    voices: Vec<Voice>,
    download: Option<(String, f32)>,
    download_rx: Option<Receiver<DownloadMsg>>,
    dirty: bool,
    _subs: Vec<Subscription>,
}

fn choice(
    labels: impl IntoIterator<Item = String>,
    selected: usize,
    window: &mut Window,
    cx: &mut Context<SettingsView>,
) -> Entity<Choice> {
    let items: Vec<SharedString> = labels.into_iter().map(SharedString::from).collect();
    cx.new(|cx| SelectState::new(SearchableVec::new(items), Some(IndexPath::new(selected)), window, cx))
}

fn text(value: &str, window: &mut Window, cx: &mut Context<SettingsView>) -> Entity<InputState> {
    let value = value.to_string();
    cx.new(|cx| InputState::new(window, cx).default_value(value))
}

impl SettingsView {
    pub fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let cfg = Config::load_or_default();
        let on_detect = choice(
            ON_DETECT.iter().map(|(_, l)| l.to_string()),
            ON_DETECT.iter().position(|(v, _)| *v == cfg.detection.on_detect).unwrap_or(0),
            window,
            cx,
        );
        let encoder = choice(
            VideoEncoder::ALL.iter().map(|e| e.label().to_string()),
            VideoEncoder::ALL.iter().position(|e| *e == cfg.capture.encoder).unwrap_or(0),
            window,
            cx,
        );
        let fps = choice(
            FPS.iter().map(|f| format!("{f} fps")),
            FPS.iter().position(|f| *f == cfg.capture.fps).unwrap_or(1),
            window,
            cx,
        );
        let device = choice(
            DEVICES.iter().map(|(_, l)| l.to_string()),
            DEVICES.iter().position(|(d, _)| *d == cfg.transcription.device).unwrap_or(0),
            window,
            cx,
        );
        let threshold = choice(
            THRESHOLDS.iter().map(|t| format!("{t:.2}")),
            THRESHOLDS.iter().position(|t| (*t - cfg.voices.threshold).abs() < 0.001).unwrap_or(2),
            window,
            cx,
        );
        let apps = text(&cfg.detection.apps.join(", "), window, cx);
        let mic_device = text(&cfg.capture.mic_device, window, cx);
        let monitor_device = text(&cfg.capture.monitor_device, window, cx);
        let terminal = text(&cfg.summary.terminal.join(" "), window, cx);
        let codex = text(&cfg.summary.codex, window, cx);
        let prompt = text(&cfg.summary.prompt, window, cx);
        let recordings_dir = text(&cfg.storage.recordings_dir.to_string_lossy(), window, cx);
        let edit = cx.new(|cx| InputState::new(window, cx));

        let mut subs = Vec::new();
        for s in [&on_detect, &encoder, &fps, &device, &threshold] {
            subs.push(cx.subscribe_in(s, window, |this, _, _: &SelectEvent<SearchableVec<SharedString>>, _, cx| {
                this.dirty = true;
                cx.notify();
            }));
        }
        for i in [&apps, &mic_device, &monitor_device, &terminal, &codex, &prompt, &recordings_dir] {
            subs.push(cx.subscribe_in(i, window, |this, _, ev: &InputEvent, _, cx| {
                if let InputEvent::Change = ev {
                    this.dirty = true;
                    cx.notify();
                }
            }));
        }
        let mut this = Self {
            cfg,
            on_detect,
            encoder,
            fps,
            device,
            threshold,
            apps,
            mic_device,
            monitor_device,
            terminal,
            codex,
            prompt,
            recordings_dir,
            edit,
            voices: Vec::new(),
            download: None,
            download_rx: None,
            dirty: false,
            _subs: subs,
        };
        this.reload_voices();
        cx.spawn(async move |this, cx| {
            loop {
                cx.background_executor().timer(Duration::from_millis(200)).await;
                if this.update(cx, |this, cx| this.poll_download(cx)).is_err() {
                    break;
                }
            }
        })
        .detach();
        this
    }

    fn reload_voices(&mut self) {
        self.voices = Db::open_default().and_then(|db| db.voices()).unwrap_or_default();
    }

    fn index(state: &Entity<Choice>, cx: &App) -> usize {
        state.read(cx).selected_index(cx).map(|ix| ix.row).unwrap_or(0)
    }

    fn value(state: &Entity<InputState>, cx: &App) -> String {
        state.read(cx).value().trim().to_string()
    }

    /// Reads every control into `self.cfg`.
    fn collect(&mut self, cx: &App) {
        let c = &mut self.cfg;
        c.detection.on_detect = ON_DETECT[Self::index(&self.on_detect, cx)].0;
        c.detection.apps =
            Self::value(&self.apps, cx).split(',').map(|a| a.trim().to_lowercase()).filter(|a| !a.is_empty()).collect();
        c.capture.encoder = VideoEncoder::ALL[Self::index(&self.encoder, cx)];
        c.capture.fps = FPS[Self::index(&self.fps, cx)];
        c.capture.mic_device = Self::value(&self.mic_device, cx);
        c.capture.monitor_device = Self::value(&self.monitor_device, cx);
        c.transcription.device = DEVICES[Self::index(&self.device, cx)].0;
        c.voices.threshold = THRESHOLDS[Self::index(&self.threshold, cx)];
        c.summary.terminal = Self::value(&self.terminal, cx).split_whitespace().map(String::from).collect();
        c.summary.codex = Self::value(&self.codex, cx);
        c.summary.prompt = Self::value(&self.prompt, cx);
        let dir = Self::value(&self.recordings_dir, cx);
        if !dir.is_empty() {
            c.storage.recordings_dir = dir.into();
        }
    }

    fn save(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.collect(cx);
        match self.cfg.save() {
            Ok(()) => {
                self.dirty = false;
                std::thread::spawn(|| {
                    let _ = actas_core::ipc::request(&Request::ReloadConfig);
                });
                window.push_notification(Notification::success("Ajustes guardados"), cx);
            }
            Err(e) => window.push_notification(Notification::error(format!("{e:#}")), cx),
        }
        cx.notify();
    }

    fn start_download(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.collect(cx);
        let missing = cli::missing_files(&self.cfg);
        let total: u64 = missing.iter().map(|f| f.approx_mb).sum();
        let me = cx.entity().downgrade();
        window.open_alert_dialog(cx, move |alert, _, _| {
            let me = me.clone();
            alert
                .confirm()
                .title("Descargar modelos")
                .description(format!(
                    "Se descargarán unos {total} MB desde Hugging Face y GitHub. Es la única operación de \
                     Actas que usa la red; tus grabaciones no salen del equipo."
                ))
                .ok_text("Descargar")
                .cancel_text("Cancelar")
                .on_ok(move |_, _, cx| {
                    let _ = me.update(cx, |this, cx| this.run_download(cx));
                    true
                })
                .on_cancel(|_, _, _| true)
        });
    }

    fn run_download(&mut self, cx: &mut Context<Self>) {
        let (tx, rx) = std::sync::mpsc::channel();
        let cfg = self.cfg.clone();
        std::thread::spawn(move || {
            let result = (|| -> anyhow::Result<()> {
                for f in cli::missing_files(&cfg) {
                    let name = f.dest.file_name().unwrap_or_default().to_string_lossy().to_string();
                    let mut last = 0u64;
                    cli::download(f.url, &f.dest, |done, total| {
                        if done - last >= 4 << 20 {
                            last = done;
                            let frac = total.map(|t| done as f32 / t as f32).unwrap_or(0.0);
                            let _ = tx.send(DownloadMsg::Progress(name.clone(), frac));
                        }
                    })?;
                }
                Ok(())
            })();
            let _ = actas_core::ipc::request(&Request::ReloadConfig);
            let _ = tx.send(DownloadMsg::Done(result.map_err(|e| format!("{e:#}"))));
        });
        self.download = Some(("preparando".into(), 0.0));
        self.download_rx = Some(rx);
        cx.notify();
    }

    fn poll_download(&mut self, cx: &mut Context<Self>) {
        let Some(rx) = &self.download_rx else { return };
        let mut finished = None;
        while let Ok(msg) = rx.try_recv() {
            match msg {
                DownloadMsg::Progress(name, f) => self.download = Some((name, f)),
                DownloadMsg::Done(r) => finished = Some(r),
            }
        }
        if let Some(r) = finished {
            self.download = None;
            self.download_rx = None;
            if let Err(e) = r {
                tracing::warn!("descarga: {e}");
                self.download = Some((format!("Error: {e}"), 0.0));
            }
        }
        cx.notify();
    }

    fn rename_voice(&mut self, id: i64, name: String, window: &mut Window, cx: &mut Context<Self>) {
        self.edit.update(cx, |s, cx| s.set_value(name, window, cx));
        let (input, me) = (self.edit.clone(), cx.entity().downgrade());
        window.open_dialog(cx, move |dialog, _, _| {
            let (input, me) = (input.clone(), me.clone());
            dialog
                .title("Renombrar voz")
                .w(px(400.))
                .child(Input::new(&input))
                .footer(
                    DialogFooter::new()
                        .child(DialogClose::new().trigger(|b| b.label("Cancelar")))
                        .child(DialogAction::new().child(Button::new("ok").primary().label("Guardar"))),
                )
                .on_ok(move |_, _, cx| {
                    let name = input.read(cx).value().trim().to_string();
                    if name.is_empty() {
                        return false;
                    }
                    let _ = me.update(cx, |this, cx| {
                        if let Ok(db) = Db::open_default() {
                            let _ = db.rename_voice(id, &name);
                        }
                        this.reload_voices();
                        cx.notify();
                    });
                    true
                })
        });
    }

    fn delete_voice(&mut self, id: i64, name: String, window: &mut Window, cx: &mut Context<Self>) {
        let me = cx.entity().downgrade();
        window.open_alert_dialog(cx, move |alert, _, _| {
            let me = me.clone();
            alert
                .confirm()
                .title(format!("¿Borrar la voz «{name}»?"))
                .description("Se borra su huella. Las reuniones donde aparecía vuelven a mostrar «Hablante N».")
                .ok_text("Borrar")
                .cancel_text("Cancelar")
                .ok_variant(ButtonVariant::Danger)
                .on_ok(move |_, _, cx| {
                    let _ = me.update(cx, |this, cx| {
                        if let Ok(db) = Db::open_default() {
                            let _ = db.delete_voice(id);
                        }
                        this.reload_voices();
                        cx.notify();
                    });
                    true
                })
                .on_cancel(|_, _, _| true)
        });
    }

    fn section(p: &Palette, title: &str) -> Div {
        div().pt_4().pb_1().font_semibold().border_b_1().border_color(p.border).child(title.to_string())
    }

    fn field(p: &Palette, label: &str, hint: Option<&str>, control: impl IntoElement) -> Div {
        h_flex()
            .gap_4()
            .items_start()
            .py_1()
            .child(
                v_flex()
                    .w(px(230.))
                    .flex_none()
                    .child(div().text_sm().child(label.to_string()))
                    .when_some(hint, |d, h| d.child(div().text_xs().text_color(p.muted).child(h.to_string()))),
            )
            .child(div().flex_1().child(control))
    }

    fn list_with_remove(
        &self,
        p: &Palette,
        id: &'static str,
        items: &[String],
        empty: &str,
        remove: fn(&mut Config, usize),
        cx: &mut Context<Self>,
    ) -> AnyElement {
        if items.is_empty() {
            return div().text_sm().text_color(p.muted).child(empty.to_string()).into_any_element();
        }
        let mut col = v_flex().gap_1();
        for (i, item) in items.iter().enumerate() {
            col = col.child(
                h_flex()
                    .gap_2()
                    .px_2()
                    .py_1()
                    .rounded(px(6.))
                    .border_1()
                    .border_color(p.border)
                    .child(div().flex_1().text_sm().child(item.clone()))
                    .child(Button::new((id, i)).xsmall().ghost().icon(IconName::Close).on_click(cx.listener(
                        move |this, _, _, cx| {
                            remove(&mut this.cfg, i);
                            this.dirty = true;
                            cx.notify();
                        },
                    ))),
            );
        }
        col.into_any_element()
    }
}

impl Render for SettingsView {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let p = Palette::get(cx);
        let d = self.cfg.detection.clone();
        let missing: Vec<_> = cli::model_files(&self.cfg).into_iter().map(|f| (f.dest.exists(), f.dest)).collect();
        let all_models = missing.iter().all(|(ok, _)| *ok);

        let mut models = v_flex().gap_0p5();
        for (ok, path) in &missing {
            models = models.child(
                div().text_xs().font_family(style::mono()).text_color(if *ok { p.ok } else { p.warning }).child(
                    format!(
                        "{} {}",
                        if *ok { "✓" } else { "✗" },
                        path.file_name().unwrap_or_default().to_string_lossy()
                    ),
                ),
            );
        }

        let mut voices = v_flex().gap_1();
        if self.voices.is_empty() {
            voices = voices.child(
                div()
                    .text_sm()
                    .text_color(p.muted)
                    .child("Ninguna. En una reunión: pestaña «Hablantes» → «Guardar voz»."),
            );
        }
        for v in &self.voices {
            let (id, name) = (v.id, v.name.clone());
            voices = voices.child(
                h_flex()
                    .gap_2()
                    .px_2()
                    .py_1()
                    .rounded(px(6.))
                    .border_1()
                    .border_color(p.border)
                    .child(div().flex_1().text_sm().child(v.name.clone()))
                    .child(
                        div()
                            .text_xs()
                            .text_color(p.muted)
                            .child(format!("{} reunión(es) · {} muestra(s)", v.meetings, v.samples)),
                    )
                    .child(Button::new(("vren", id as usize)).xsmall().ghost().label("Renombrar").on_click({
                        let name = name.clone();
                        cx.listener(move |this, _, window, cx| this.rename_voice(id, name.clone(), window, cx))
                    }))
                    .child(Button::new(("vdel", id as usize)).xsmall().ghost().label("Borrar").on_click({
                        let name = name.clone();
                        cx.listener(move |this, _, window, cx| this.delete_voice(id, name.clone(), window, cx))
                    })),
            );
        }

        let content = v_flex()
            .max_w(px(820.))
            .gap_1()
            .child(Self::section(&p, "Detección de llamadas"))
            .child(Self::field(
                &p,
                "Uso del micrófono (PipeWire)",
                Some("Una app de la lista abre el micrófono durante 5 s."),
                Switch::new("pw").checked(d.pipewire_mic).on_click(cx.listener(|this, v: &bool, _, cx| {
                    this.cfg.detection.pipewire_mic = *v;
                    this.dirty = true;
                    cx.notify();
                })),
            ))
            .child(Self::field(
                &p,
                "Título de ventana (Hyprland)",
                Some("«Meet – …», «… | Microsoft Teams», «Zoom Meeting». Da nombre a la grabación."),
                Switch::new("title").checked(d.window_title).on_click(cx.listener(|this, v: &bool, _, cx| {
                    this.cfg.detection.window_title = *v;
                    this.dirty = true;
                    cx.notify();
                })),
            ))
            .child(Self::field(&p, "Al detectar una llamada", None, Select::new(&self.on_detect).w(px(260.))))
            .child(Self::field(
                &p,
                "Apps vigiladas",
                Some("Separadas por comas; se comparan con el nombre del proceso."),
                Input::new(&self.apps),
            ))
            .child(Self::field(
                &p,
                "Grabar siempre estos títulos",
                None,
                self.list_with_remove(&p, "auto", &d.auto_record_titles, "Ninguno.", |c, i| {
                    c.detection.auto_record_titles.remove(i);
                }, cx),
            ))
            .child(Self::field(
                &p,
                "No sugerir con estas apps",
                None,
                self.list_with_remove(&p, "muted", &d.muted_apps, "Ninguna.", |c, i| {
                    c.detection.muted_apps.remove(i);
                }, cx),
            ))
            .child(Self::section(&p, "Captura"))
            .child(Self::field(
                &p,
                "Codificador de vídeo",
                Some("Automático prueba NVENC, después VA-API y por último software. Cámbialo si un reproductor no abre las grabaciones."),
                Select::new(&self.encoder).w(px(320.)),
            ))
            .child(Self::field(&p, "Fotogramas por segundo", None, Select::new(&self.fps).w(px(160.))))
            .child(Self::field(
                &p,
                "Recordar la ventana elegida",
                Some("El portal no vuelve a preguntar qué capturar."),
                h_flex()
                    .gap_3()
                    .child(Switch::new("remember").checked(self.cfg.capture.remember_source).on_click(cx.listener(
                        |this, v: &bool, _, cx| {
                            this.cfg.capture.remember_source = *v;
                            this.dirty = true;
                            cx.notify();
                        },
                    )))
                    .child(Button::new("forget").small().label("Elegir otra ventana").on_click(|_, window, cx| {
                        let _ = std::fs::remove_file(
                            actas_core::paths::state_dir().join("screencast-restore-token"),
                        );
                        window.push_notification(
                            Notification::new().message("La próxima grabación preguntará qué capturar."),
                            cx,
                        );
                    })),
            ))
            .child(Self::field(
                &p,
                "Micrófono",
                Some("Nombre de la fuente de PulseAudio/PipeWire; vacío = la predeterminada. Pista «Yo»."),
                Input::new(&self.mic_device),
            ))
            .child(Self::field(
                &p,
                "Audio del sistema",
                Some("Vacío = monitor de la salida predeterminada. Pista «los demás»."),
                Input::new(&self.monitor_device),
            ))
            .child(Self::section(&p, "Transcripción"))
            .child(Self::field(
                &p,
                "Modelo",
                Some("Parakeet TDT 0.6B v3 + Sortformer (hasta 8 voces) + CAM++ para huellas de voz."),
                v_flex()
                    .gap_2()
                    .child(models)
                    .when(!all_models && self.download.is_none(), |d| {
                        d.child(Button::new("download").small().primary().label("Descargar modelos (≈3 GB)").on_click(
                            cx.listener(|this, _, window, cx| this.start_download(window, cx)),
                        ))
                    })
                    .when_some(self.download.clone(), |d, (name, f)| {
                        d.child(div().text_xs().text_color(p.muted).child(format!("{name} · {:.0} %", f * 100.0)))
                            .child(style::progress(&p, f))
                    }),
            ))
            .child(Self::field(
                &p,
                "Dispositivo",
                Some("Si CUDA falla, se usa la CPU automáticamente."),
                Select::new(&self.device).w(px(200.)),
            ))
            .child(Self::section(&p, "Voces guardadas"))
            .child(Self::field(&p, "Voces", Some("Solo en este equipo."), voices))
            .child(Self::field(
                &p,
                "Umbral de reconocimiento",
                Some("Más alto = menos falsos positivos; por debajo, el hablante queda como desconocido."),
                Select::new(&self.threshold).w(px(160.)),
            ))
            .child(Self::section(&p, "Resumen → Notion (Codex)"))
            .child(
                div()
                    .text_sm()
                    .text_color(p.warning)
                    .py_1()
                    .child("La transcripción sale del equipo hacia Codex/OpenAI y Notion. Solo cuando pulses el botón."),
            )
            .child(Self::field(&p, "Terminal", Some("Programa y argumentos."), Input::new(&self.terminal)))
            .child(Self::field(&p, "Orden de Codex", None, Input::new(&self.codex)))
            .child(Self::field(
                &p,
                "Primer mensaje",
                Some("{title} y {transcript} se sustituyen. El destino en Notion se decide en el chat."),
                Input::new(&self.prompt),
            ))
            .child(Self::section(&p, "Almacenamiento"))
            .child(Self::field(&p, "Carpeta de grabaciones", None, Input::new(&self.recordings_dir)));

        v_flex()
            .flex_1()
            .size_full()
            .child(
                h_flex()
                    .px_6()
                    .py_3()
                    .gap_2()
                    .border_b_1()
                    .border_color(p.border)
                    .child(div().text_xl().font_semibold().child("Ajustes"))
                    .child(div().flex_1())
                    .when(self.dirty, |d| d.child(div().text_sm().text_color(p.warning).child("Cambios sin guardar")))
                    .child(
                        Button::new("save")
                            .primary()
                            .small()
                            .label("Guardar")
                            .disabled(!self.dirty)
                            .on_click(cx.listener(|this, _, window, cx| this.save(window, cx))),
                    ),
            )
            .child(div().flex_1().px_6().pb_6().overflow_y_scrollbar().child(content))
    }
}
