//! Daemon state and commands. Every entry point (IPC, tray, notifications,
//! detector) goes through [`Daemon`].

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::Instant;

use actas_core::config::Config;
use actas_core::db::{Meeting, Status};
use actas_core::ipc::{DaemonStatus, Event, JobStatus, RecordingStatus, Suggestion};
use actas_core::{Db, paths};
use anyhow::{Context, Result, bail};
use tokio::sync::broadcast;

use crate::detector::{Action, Call, Detector, TitleMatcher};
use crate::notify::{Notification, Notifier};
use crate::recorder::{Recorder, RecorderEvent};
use crate::{portal, tray, worker};

struct ActiveRecording {
    meeting_id: String,
    title: String,
    platform: Option<String>,
    recorder: Recorder,
    portal: Option<portal::Session>,
    call_ended: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NotifKind {
    Suggestion,
    CallEnded,
}

struct State {
    config: Config,
    rec: Option<ActiveRecording>,
    starting: bool,
    detector: Detector,
    matcher: TitleMatcher,
    suggestion: Option<Call>,
    notifications: HashMap<u32, NotifKind>,
    job: Option<JobStatus>,
    queued: usize,
    missing_models: Vec<String>,
}

struct Inner {
    state: Mutex<State>,
    db: Mutex<Db>,
    events: broadcast::Sender<Event>,
    worker: std::sync::mpsc::Sender<worker::Msg>,
    notifier: Option<Notifier>,
    tray: OnceLock<ksni::Handle<tray::Tray>>,
    rt: tokio::runtime::Handle,
}

#[derive(Clone)]
pub struct Daemon(Arc<Inner>);

fn disk_free(path: &std::path::Path) -> u64 {
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_bytes()) else { return 0 };
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return 0;
    }
    st.f_bavail as u64 * st.f_frsize as u64
}

fn new_meeting_id() -> String {
    chrono::Local::now().format("%Y%m%d-%H%M%S").to_string()
}

impl Daemon {
    pub fn new(
        config: Config,
        db: Db,
        notifier: Option<Notifier>,
        worker: std::sync::mpsc::Sender<worker::Msg>,
        rt: tokio::runtime::Handle,
    ) -> Self {
        let (events, _) = broadcast::channel(256);
        let missing_models = actas_engine::missing_models(&config.transcription);
        let queued = db.meetings().map(|m| m.iter().filter(|m| m.status == Status::Queued).count()).unwrap_or(0);
        let state = State {
            matcher: TitleMatcher::new(&config.detection.title_patterns),
            config,
            rec: None,
            starting: false,
            detector: Detector::default(),
            suggestion: None,
            notifications: HashMap::new(),
            job: None,
            queued,
            missing_models,
        };
        Self(Arc::new(Inner {
            state: Mutex::new(state),
            db: Mutex::new(db),
            events,
            worker,
            notifier,
            tray: OnceLock::new(),
            rt,
        }))
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.0.state.lock().unwrap()
    }

    pub fn db(&self) -> MutexGuard<'_, Db> {
        self.0.db.lock().unwrap()
    }

    pub fn config(&self) -> Config {
        self.state().config.clone()
    }

    pub fn set_tray(&self, handle: ksni::Handle<tray::Tray>) {
        let _ = self.0.tray.set(handle);
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.0.events.subscribe()
    }

    pub fn status(&self) -> DaemonStatus {
        let st = self.state();
        let recording = st.rec.as_ref().map(|r| {
            let levels = *r.recorder.levels.lock().unwrap();
            RecordingStatus {
                meeting_id: r.meeting_id.clone(),
                title: r.title.clone(),
                platform: r.platform.clone(),
                elapsed_ms: r.recorder.elapsed_ms(),
                paused: r.recorder.is_paused(),
                video: r.recorder.has_video(),
                source: r.recorder.source_label.clone(),
                encoder: r.recorder.encoder.clone(),
                mic_level: levels.mic,
                system_level: levels.system,
                bytes_written: r.recorder.bytes_written(),
                disk_free: disk_free(r.recorder.dir()),
                call_ended: r.call_ended,
            }
        });
        DaemonStatus {
            recording,
            suggestion: st.suggestion.as_ref().map(|c| Suggestion {
                app: c.app.clone(),
                title: c.title.clone().or(c.raw_title.clone()),
                platform: c.platform.clone(),
            }),
            transcribing: st.job.clone(),
            queued: st.queued,
            missing_models: st.missing_models.clone(),
        }
    }

    /// Pushes the current status to subscribers and the tray.
    pub fn emit_status(&self) {
        let status = self.status();
        if let Some(tray) = self.0.tray.get() {
            let s = status.clone();
            let tray = tray.clone();
            self.0.rt.spawn(async move {
                tray.update(|t| t.status = s).await;
            });
        }
        let _ = self.0.events.send(Event::Status(status));
    }

    pub fn emit_library(&self, meeting_id: &str) {
        let _ = self.0.events.send(Event::LibraryChanged { meeting_id: meeting_id.to_string() });
    }

    pub fn is_recording(&self) -> bool {
        self.state().rec.is_some()
    }

    async fn notify(&self, n: Notification<'_>, kind: Option<NotifKind>) -> Option<u32> {
        let notifier = self.0.notifier.as_ref()?;
        match notifier.send(n).await {
            Ok(id) => {
                if let Some(k) = kind {
                    self.state().notifications.insert(id, k);
                }
                Some(id)
            }
            Err(e) => {
                tracing::warn!("no pude mostrar la notificación: {e:#}");
                None
            }
        }
    }

    async fn close_notifications(&self, kind: NotifKind) {
        let ids: Vec<u32> = {
            let mut st = self.state();
            let ids = st.notifications.iter().filter(|(_, k)| **k == kind).map(|(id, _)| *id).collect::<Vec<_>>();
            st.notifications.retain(|_, k| *k != kind);
            ids
        };
        if let Some(n) = &self.0.notifier {
            for id in ids {
                n.close(id).await;
            }
        }
    }

    // ---------- recording ----------

    pub async fn start(&self, title: Option<String>, video: bool) -> Result<()> {
        let (cfg, call) = {
            let mut st = self.state();
            if st.rec.is_some() || st.starting {
                bail!("ya hay una grabación en curso");
            }
            st.starting = true;
            st.detector.recording_started();
            (st.config.clone(), st.suggestion.take().or_else(|| st.detector.current().cloned()))
        };
        self.close_notifications(NotifKind::Suggestion).await;
        let result = self.start_inner(&cfg, title, video, call).await;
        self.state().starting = false;
        if let Err(e) = &result {
            self.notify(
                Notification {
                    summary: "No se pudo empezar a grabar",
                    body: &format!("{e:#}"),
                    actions: &[],
                    resident: false,
                    replaces: None,
                },
                None,
            )
            .await;
        }
        self.emit_status();
        result
    }

    async fn start_inner(&self, cfg: &Config, title: Option<String>, video: bool, call: Option<Call>) -> Result<()> {
        let id = new_meeting_id();
        let dir = cfg.storage.recordings_dir.join(&id);
        std::fs::create_dir_all(&dir).with_context(|| format!("creando {}", dir.display()))?;
        let title = title
            .filter(|t| !t.trim().is_empty())
            .or_else(|| call.as_ref().and_then(|c| c.title.clone()))
            .unwrap_or_else(|| format!("Reunión {}", chrono::Local::now().format("%d/%m %H:%M")));
        let platform = call.as_ref().and_then(|c| c.platform.clone());

        let (portal, source) = if video {
            match portal::open_with_retry(cfg.capture.remember_source).await {
                Ok((session, source)) => (Some(session), Some(source)),
                Err(e) => {
                    tracing::warn!("sin captura de pantalla: {e:#}");
                    self.notify(
                        Notification {
                            summary: "Grabo solo el audio",
                            body: &format!("No se pudo capturar la pantalla: {e:#}"),
                            actions: &[],
                            resident: false,
                            replaces: None,
                        },
                        None,
                    )
                    .await;
                    (None, None)
                }
            }
        } else {
            (None, None)
        };

        let this = self.clone();
        let on_event = move |ev: RecorderEvent| {
            let this = this.clone();
            this.0.rt.clone().spawn(async move { this.on_recorder_event(ev).await });
        };
        let recorder = {
            let dir = dir.clone();
            let capture = cfg.capture.clone();
            tokio::task::spawn_blocking(move || Recorder::start(&dir, &capture, source, on_event)).await??
        };
        let meeting = Meeting {
            id: id.clone(),
            title: title.clone(),
            platform: platform.clone(),
            app: call.as_ref().map(|c| c.app.clone()),
            started_at: chrono::Utc::now().timestamp_millis(),
            duration_ms: 0,
            dir,
            has_video: recorder.has_video(),
            status: Status::Recording,
            progress: 0.0,
            stage: None,
            error: None,
            model: None,
        };
        self.db().create_meeting(&meeting)?;
        tracing::info!("grabando «{title}» en {}", meeting.dir.display());
        self.state().rec =
            Some(ActiveRecording { meeting_id: id.clone(), title, platform, recorder, portal, call_ended: false });
        self.emit_library(&id);
        Ok(())
    }

    async fn on_recorder_event(&self, ev: RecorderEvent) {
        match ev {
            RecorderEvent::VideoLost(reason) => {
                let had_video = {
                    let mut st = self.state();
                    match st.rec.as_mut() {
                        Some(r) if r.recorder.has_video() => {
                            r.recorder.drop_video();
                            true
                        }
                        _ => false,
                    }
                };
                if had_video {
                    self.notify(
                        Notification {
                            summary: "Se perdió la captura de pantalla",
                            body: &format!("Sigo grabando el audio. ({reason})"),
                            actions: &[],
                            resident: false,
                            replaces: None,
                        },
                        None,
                    )
                    .await;
                    self.emit_status();
                }
            }
            RecorderEvent::AudioFailed(reason) => {
                tracing::error!("la captura de audio falló: {reason}");
                let _ = self.stop().await;
                self.notify(
                    Notification {
                        summary: "La grabación se detuvo",
                        body: &reason,
                        actions: &[],
                        resident: false,
                        replaces: None,
                    },
                    None,
                )
                .await;
            }
        }
    }

    pub async fn stop(&self) -> Result<()> {
        let Some(rec) = self.state().rec.take() else { bail!("no hay ninguna grabación en curso") };
        self.close_notifications(NotifKind::CallEnded).await;
        self.emit_status();
        let ActiveRecording { meeting_id, title, recorder, portal, .. } = rec;
        let result = tokio::task::spawn_blocking(move || recorder.stop()).await?;
        if let Some(p) = portal {
            p.close().await;
        }
        match result {
            Ok(duration) => {
                self.db().finish_recording(&meeting_id, duration, Status::Queued)?;
                tracing::info!("«{title}» guardada ({})", actas_core::fmt_ms(duration));
                self.state().queued += 1;
                let _ = self.0.worker.send(worker::Msg::Wake);
            }
            Err(e) => {
                self.db().set_status(&meeting_id, Status::Failed, Some(&format!("{e:#}")))?;
            }
        }
        self.emit_library(&meeting_id);
        self.emit_status();
        Ok(())
    }

    pub async fn toggle(&self) -> Result<()> {
        if self.is_recording() { self.stop().await } else { self.start(None, true).await }
    }

    pub fn toggle_pause(&self) -> Result<()> {
        {
            let mut st = self.state();
            let rec = st.rec.as_mut().context("no hay ninguna grabación en curso")?;
            let paused = !rec.recorder.is_paused();
            rec.recorder.set_paused(paused)?;
        }
        self.emit_status();
        Ok(())
    }

    pub fn mark(&self, label: Option<String>) -> Result<()> {
        let (id, at) = {
            let st = self.state();
            let rec = st.rec.as_ref().context("no hay ninguna grabación en curso")?;
            (rec.meeting_id.clone(), rec.recorder.elapsed_ms())
        };
        let n = self.db().marks(&id)?.len() + 1;
        let label = label.filter(|l| !l.trim().is_empty()).unwrap_or_else(|| format!("Marca {n}"));
        self.db().add_mark(&id, at, &label)?;
        self.emit_library(&id);
        Ok(())
    }

    // ---------- detection ----------

    pub async fn accept_suggestion(&self, video: bool) -> Result<()> {
        self.start(None, video).await
    }

    pub async fn dismiss_suggestion(&self) {
        {
            let mut st = self.state();
            st.suggestion = None;
            let cfg = st.config.detection.clone();
            st.detector.snooze(Instant::now(), &cfg);
        }
        self.close_notifications(NotifKind::Suggestion).await;
        self.emit_status();
    }

    /// "Siempre": remember the title and record now.
    async fn always_record(&self) -> Result<()> {
        let title = {
            let st = self.state();
            st.suggestion.as_ref().and_then(|c| c.title.clone().or(c.raw_title.clone()))
        };
        if let Some(t) = title {
            let mut cfg = self.config();
            if !cfg.detection.auto_record_titles.iter().any(|a| a.eq_ignore_ascii_case(&t)) {
                cfg.detection.auto_record_titles.push(t);
                cfg.save()?;
                self.state().config = cfg;
            }
        }
        self.start(None, true).await
    }

    /// One detection step; called every couple of seconds.
    pub async fn detect_once(&self) {
        let (cfg, matcher_patterns) = {
            let st = self.state();
            (st.config.detection.clone(), st.config.detection.title_patterns.clone())
        };
        if !cfg.pipewire_mic && !cfg.window_title {
            return;
        }
        let obs = {
            let cfg = cfg.clone();
            tokio::task::spawn_blocking(move || {
                crate::detector::observe_system(&cfg, &TitleMatcher::new(&matcher_patterns))
            })
            .await
            .unwrap_or_default()
        };
        let actions = {
            let mut st = self.state();
            let recording = st.rec.is_some() || st.starting;
            st.detector.observe(Instant::now(), &obs, recording, &cfg)
        };
        for action in actions {
            self.handle_action(action).await;
        }
    }

    async fn handle_action(&self, action: Action) {
        match action {
            Action::Suggest(call) => {
                tracing::info!("llamada detectada: {call:?}");
                let body = match (&call.title, &call.raw_title) {
                    (Some(t), _) => format!("{} usa el micrófono · «{t}»", call.app),
                    (None, Some(raw)) => format!("{} usa el micrófono · ventana «{raw}»", call.app),
                    _ => format!("{} usa el micrófono", call.app),
                };
                let summary = match &call.platform {
                    Some(p) => format!("Llamada de {p} detectada"),
                    None => "Llamada detectada".to_string(),
                };
                self.state().suggestion = Some(call.clone());
                let mut actions = vec![("record", "● Grabar"), ("audio", "Solo audio")];
                if call.title.is_some() || call.raw_title.is_some() {
                    actions.push(("always", "Siempre con este título"));
                }
                actions.push(("dismiss", "Ahora no"));
                self.notify(
                    Notification { summary: &summary, body: &body, actions: &actions, resident: true, replaces: None },
                    Some(NotifKind::Suggestion),
                )
                .await;
                self.emit_status();
            }
            Action::AutoRecord(call) => {
                tracing::info!("grabación automática: {call:?}");
                self.state().suggestion = Some(call);
                if self.start(None, true).await.is_ok() {
                    let title = self.status().recording.map(|r| r.title).unwrap_or_default();
                    self.notify(
                        Notification {
                            summary: "Grabando la reunión",
                            body: &format!("«{title}» · regla «grabar siempre»"),
                            actions: &[("stop", "Detener")],
                            resident: false,
                            replaces: None,
                        },
                        Some(NotifKind::CallEnded),
                    )
                    .await;
                }
            }
            Action::Withdraw => {
                self.state().suggestion = None;
                self.close_notifications(NotifKind::Suggestion).await;
                self.emit_status();
            }
            Action::CallEnded => {
                if let Some(r) = self.state().rec.as_mut() {
                    r.call_ended = true;
                }
                self.notify(
                    Notification {
                        summary: "La llamada parece haber terminado",
                        body: "Nadie usa ya el micrófono desde la app de la reunión.",
                        actions: &[("stop", "■ Detener"), ("continue", "Seguir grabando")],
                        resident: true,
                        replaces: None,
                    },
                    Some(NotifKind::CallEnded),
                )
                .await;
                self.emit_status();
            }
        }
    }

    pub async fn on_notification_action(&self, id: u32, key: String) {
        let kind = self.state().notifications.remove(&id);
        let result = match (kind, key.as_str()) {
            (Some(NotifKind::Suggestion), "record") => self.accept_suggestion(true).await,
            (Some(NotifKind::Suggestion), "audio") => self.accept_suggestion(false).await,
            (Some(NotifKind::Suggestion), "always") => self.always_record().await,
            (Some(NotifKind::Suggestion), "dismiss") => {
                self.dismiss_suggestion().await;
                Ok(())
            }
            (Some(NotifKind::CallEnded), "stop") => self.stop().await,
            _ => Ok(()),
        };
        if let Err(e) = result {
            tracing::warn!("acción «{key}»: {e:#}");
        }
    }

    // ---------- transcription ----------

    pub fn enqueue(&self, meeting_id: &str) -> Result<()> {
        let db = self.db();
        let m = db.meeting(meeting_id)?.context("esa reunión no existe")?;
        if matches!(m.status, Status::Recording | Status::Transcribing) {
            bail!("la reunión todavía se está procesando");
        }
        db.set_status(meeting_id, Status::Queued, None)?;
        drop(db);
        self.state().queued += 1;
        let _ = self.0.worker.send(worker::Msg::Wake);
        self.emit_library(meeting_id);
        self.emit_status();
        Ok(())
    }

    pub fn set_job(&self, job: Option<JobStatus>, queued: usize) {
        {
            let mut st = self.state();
            st.job = job;
            st.queued = queued;
            st.missing_models = actas_engine::missing_models(&st.config.transcription);
        }
        self.emit_status();
    }

    pub fn reload_config(&self) -> Result<()> {
        let cfg = Config::load()?;
        {
            let mut st = self.state();
            st.matcher = TitleMatcher::new(&cfg.detection.title_patterns);
            st.missing_models = actas_engine::missing_models(&cfg.transcription);
            st.config = cfg;
        }
        let _ = self.0.worker.send(worker::Msg::Wake);
        self.emit_status();
        Ok(())
    }

    pub fn forget_screen_source(&self) {
        portal::forget_source();
    }

    pub fn spawn<F>(&self, f: impl FnOnce(Daemon) -> F)
    where
        F: std::future::Future<Output = ()> + Send + 'static,
    {
        self.0.rt.spawn(f(self.clone()));
    }

    /// Stops a running recording cleanly before exit.
    pub async fn shutdown(&self) {
        if self.is_recording() {
            let _ = self.stop().await;
        }
        let _ = self.0.worker.send(worker::Msg::Quit);
        let _ = std::fs::remove_file(paths::socket_path());
    }
}
