//! Capture with GStreamer.
//!
//! Two pipelines share one clock and base time so their timestamps line up:
//!
//! * **audio**: mic and the default sink monitor, each written as a 16 kHz
//!   WAV for the ASR engine and as an Opus track in `audio.mka`.
//! * **video** (optional): the portal's PipeWire stream → hardware encoder →
//!   `video.mkv`.
//!
//! Keeping video separate means a closed window or a portal failure stops the
//! video only; the audio carries on. On stop both are remuxed into
//! `recording.mkv` with ffmpeg.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use actas_core::config::{Capture, VideoEncoder};
use anyhow::{Context, Result, anyhow, bail};
use gstreamer as gst;
use gstreamer::prelude::*;

/// Peak levels (0..1) updated from `level` messages.
#[derive(Debug, Default, Clone, Copy)]
pub struct Levels {
    pub mic: f32,
    pub system: f32,
}

/// Things the bus threads report back to the daemon.
#[derive(Debug)]
pub enum RecorderEvent {
    /// The video pipeline failed; audio keeps recording.
    VideoLost(String),
    /// The audio pipeline failed: the recording cannot continue.
    AudioFailed(String),
}

pub struct VideoSource {
    pub fd: std::os::fd::OwnedFd,
    pub node_id: u32,
    pub label: String,
}

pub struct Recorder {
    dir: PathBuf,
    audio: Watched,
    video: Option<Watched>,
    pub encoder: Option<String>,
    pub source_label: Option<String>,
    pub levels: Arc<Mutex<Levels>>,
    paused: bool,
    /// Kept open for the lifetime of the video pipeline.
    _video_fd: Option<std::os::fd::OwnedFd>,
}

pub fn init() -> Result<()> {
    gst::init().context("iniciando GStreamer")
}

fn has(element: &str) -> bool {
    gst::ElementFactory::find(element).is_some()
}

/// `(encoder pipeline fragment, label)` for the configured encoder, falling
/// back along NVENC → VA-API → x264 when the choice is `Auto` or unavailable.
pub fn pick_encoder(choice: VideoEncoder) -> Option<(String, String)> {
    let nvenc = |codec: &str, el: &str, parse: &str| {
        has(el).then(|| (format!("{el} bitrate=2500 gop-size=60 ! {parse}"), format!("NVENC {codec}")))
    };
    let candidates: Vec<Option<(String, String)>> = match choice {
        VideoEncoder::NvencH264 => vec![nvenc("H.264", "nvh264enc", "h264parse")],
        VideoEncoder::NvencH265 => vec![nvenc("H.265", "nvh265enc", "h265parse")],
        VideoEncoder::NvencAv1 => vec![nvenc("AV1", "nvav1enc", "av1parse")],
        VideoEncoder::VaapiH264 => {
            vec![has("vah264enc").then(|| ("vah264enc bitrate=2500 ! h264parse".into(), "VA-API H.264".into()))]
        }
        VideoEncoder::X264 | VideoEncoder::Auto => vec![],
    };
    let fallback = [
        nvenc("H.264", "nvh264enc", "h264parse"),
        has("vah264enc").then(|| ("vah264enc bitrate=2500 ! h264parse".into(), "VA-API H.264".into())),
        has("x264enc").then(|| {
            (
                "x264enc tune=zerolatency speed-preset=veryfast bitrate=2500 key-int-max=60 ! h264parse".into(),
                "x264 (software)".into(),
            )
        }),
    ];
    let chosen = candidates.into_iter().flatten().next();
    if chosen.is_none() && choice != VideoEncoder::Auto && choice != VideoEncoder::X264 {
        tracing::warn!("el codificador {choice:?} no está disponible; elijo otro");
    }
    chosen.or_else(|| {
        if choice == VideoEncoder::X264 { fallback[2].clone() } else { fallback.into_iter().flatten().next() }
    })
}

fn quote(p: &Path) -> String {
    format!("\"{}\"", p.to_string_lossy().replace('\\', "\\\\").replace('"', "\\\""))
}

/// One capture branch. PipeWire suspends an idle sink, and its monitor then
/// delivers nothing; `audiorate` turns those gaps (visible in the timestamps)
/// into silence so `mic.wav` and `system.wav` stay aligned sample by sample.
fn audio_branch(name: &str, device: &str, client: &str, dir: &Path, mux_pad: &str) -> String {
    let device = if device.is_empty() { String::new() } else { format!("device=\"{device}\" ") };
    format!(
        "pulsesrc {device}client-name=\"{client}\" do-timestamp=true provide-clock=false \
         ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 \
         ! level name={name}level interval=100000000 post-messages=true \
         ! tee name={name}tee \
         {name}tee. ! queue max-size-time=3000000000 ! opusenc bitrate=64000 ! {mux_pad} \
         {name}tee. ! queue max-size-time=3000000000 ! audioresample ! audio/x-raw,format=S16LE,rate=16000,channels=1 \
         ! audiorate skip-to-first=false ! wavenc ! filesink location={}",
        quote(&dir.join(format!("{name}.wav")))
    )
}

fn audio_pipeline(dir: &Path, cfg: &Capture) -> String {
    let monitor = if cfg.monitor_device.is_empty() { "@DEFAULT_MONITOR@" } else { &cfg.monitor_device };
    format!(
        "matroskamux name=amux ! filesink location={} {} {}",
        quote(&dir.join("audio.mka")),
        audio_branch("mic", &cfg.mic_device, "Actas · micrófono", dir, "amux."),
        audio_branch("system", monitor, "Actas · sistema", dir, "amux."),
    )
}

fn video_pipeline(dir: &Path, src: &VideoSource, fps: u32, encoder: &str) -> String {
    use std::os::fd::AsRawFd;
    format!(
        "pipewiresrc fd={} path={} do-timestamp=true keepalive-time=1000 always-copy=true \
         ! videoconvert ! videorate ! video/x-raw,framerate={fps}/1 \
         ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=3000000000 \
         ! {encoder} ! queue ! matroskamux ! filesink location={}",
        src.fd.as_raw_fd(),
        src.node_id,
        quote(&dir.join("video.mkv"))
    )
}

fn parse(desc: &str) -> Result<gst::Pipeline> {
    gst::parse::launch(desc)
        .with_context(|| format!("pipeline inválido: {desc}"))?
        .downcast::<gst::Pipeline>()
        .map_err(|_| anyhow!("no es un pipeline"))
}

/// Set by the bus thread once the pipeline reached EOS or failed.
#[derive(Default)]
struct Finished {
    done: Mutex<bool>,
    cv: std::sync::Condvar,
}

impl Finished {
    fn set(&self) {
        *self.done.lock().unwrap() = true;
        self.cv.notify_all();
    }

    fn wait(&self, timeout: Duration) -> bool {
        let guard = self.done.lock().unwrap();
        let (guard, _) = self.cv.wait_timeout_while(guard, timeout, |d| !*d).unwrap();
        *guard
    }
}

/// Watches a pipeline bus on a thread: levels, errors, EOS.
fn watch_bus(
    pipeline: &gst::Pipeline,
    levels: Option<Arc<Mutex<Levels>>>,
    on_error: impl Fn(String) + Send + 'static,
) -> Arc<Finished> {
    let bus = pipeline.bus().expect("pipeline sin bus");
    let finished = Arc::new(Finished::default());
    let fin = finished.clone();
    std::thread::Builder::new()
        .name("actas-bus".into())
        .spawn(move || {
            for msg in bus.iter_timed(gst::ClockTime::NONE) {
                use gst::MessageView;
                match msg.view() {
                    MessageView::Element(e) => {
                        let (Some(levels), Some(s)) = (&levels, e.structure()) else { continue };
                        if s.name() != "level" {
                            continue;
                        }
                        let peak = s
                            .get::<gst::glib::ValueArray>("peak")
                            .ok()
                            .and_then(|a| a.iter().filter_map(|v| v.get::<f64>().ok()).reduce(f64::max))
                            .map(|db| 10f64.powf(db / 20.0).clamp(0.0, 1.0) as f32)
                            .unwrap_or(0.0);
                        let src = msg.src().map(|s| s.name().to_string()).unwrap_or_default();
                        let mut l = levels.lock().unwrap();
                        if src.starts_with("mic") {
                            l.mic = peak;
                        } else {
                            l.system = peak;
                        }
                    }
                    MessageView::Error(err) => {
                        let text =
                            format!("{}: {}", msg.src().map(|s| s.name().to_string()).unwrap_or_default(), err.error());
                        tracing::error!("GStreamer: {text} ({:?})", err.debug());
                        fin.set();
                        on_error(text);
                        break;
                    }
                    MessageView::Eos(_) => {
                        fin.set();
                        break;
                    }
                    _ => {}
                }
            }
        })
        .expect("hilo del bus");
    finished
}

struct Watched {
    pipeline: gst::Pipeline,
    finished: Arc<Finished>,
}

impl Watched {
    /// Sends EOS so muxers and WAV headers are finalised, then stops.
    fn stop(self, timeout: Duration) {
        self.pipeline.send_event(gst::event::Eos::new());
        if !self.finished.wait(timeout) {
            tracing::warn!("la captura no terminó a tiempo; la corto");
        }
        let _ = self.pipeline.set_state(gst::State::Null);
    }
}

impl Recorder {
    /// Starts capturing into `dir`. `events` receives failures from the bus
    /// threads.
    pub fn start(
        dir: &Path,
        cfg: &Capture,
        video: Option<VideoSource>,
        events: impl Fn(RecorderEvent) + Send + Sync + Clone + 'static,
    ) -> Result<Self> {
        std::fs::create_dir_all(dir)?;
        let levels = Arc::new(Mutex::new(Levels::default()));
        let audio = parse(&audio_pipeline(dir, cfg))?;
        let clock = gst::SystemClock::obtain();
        audio.use_clock(Some(&clock));
        audio.set_start_time(gst::ClockTime::NONE);
        let base = clock.time();
        audio.set_base_time(base);
        let finished = {
            let ev = events.clone();
            watch_bus(&audio, Some(levels.clone()), move |e| ev(RecorderEvent::AudioFailed(e)))
        };
        audio.set_state(gst::State::Playing).context("arrancando la captura de audio")?;
        let audio = Watched { pipeline: audio, finished };

        let mut rec = Self {
            dir: dir.to_path_buf(),
            audio,
            video: None,
            encoder: None,
            source_label: None,
            levels,
            paused: false,
            _video_fd: None,
        };

        if let Some(src) = video {
            match pick_encoder(cfg.encoder) {
                None => {
                    events(RecorderEvent::VideoLost("no hay ningún codificador de vídeo disponible".into()));
                }
                Some((enc, label)) => {
                    let started = parse(&video_pipeline(dir, &src, cfg.fps.clamp(1, 60), &enc)).and_then(|p| {
                        p.use_clock(Some(&clock));
                        p.set_start_time(gst::ClockTime::NONE);
                        p.set_base_time(base);
                        let ev = events.clone();
                        let finished = watch_bus(&p, None, move |e| ev(RecorderEvent::VideoLost(e)));
                        p.set_state(gst::State::Playing).context("arrancando la captura de vídeo")?;
                        Ok(Watched { pipeline: p, finished })
                    });
                    match started {
                        Ok(p) => {
                            rec.video = Some(p);
                            rec.encoder = Some(label);
                            rec.source_label = Some(src.label.clone());
                            rec._video_fd = Some(src.fd);
                        }
                        Err(e) => events(RecorderEvent::VideoLost(format!("{e:#}"))),
                    }
                }
            }
        }
        Ok(rec)
    }

    pub fn has_video(&self) -> bool {
        self.video.is_some()
    }

    /// Stops the video pipeline after a failure, keeping the audio running.
    pub fn drop_video(&mut self) {
        if let Some(v) = self.video.take() {
            let _ = v.pipeline.set_state(gst::State::Null);
            self.encoder = None;
            self.source_label = None;
            self._video_fd = None;
        }
    }

    pub fn elapsed_ms(&self) -> i64 {
        self.audio.pipeline.current_running_time().map(|t| t.mseconds() as i64).unwrap_or(0)
    }

    pub fn is_paused(&self) -> bool {
        self.paused
    }

    pub fn set_paused(&mut self, paused: bool) -> Result<()> {
        let state = if paused { gst::State::Paused } else { gst::State::Playing };
        for w in std::iter::once(&self.audio).chain(self.video.as_ref()) {
            w.pipeline.set_state(state).context("cambiando de estado la captura")?;
        }
        self.paused = paused;
        if paused {
            *self.levels.lock().unwrap() = Levels::default();
        }
        Ok(())
    }

    pub fn bytes_written(&self) -> u64 {
        std::fs::read_dir(&self.dir)
            .map(|d| d.flatten().filter_map(|e| e.metadata().ok()).map(|m| m.len()).sum())
            .unwrap_or(0)
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Finishes the files (EOS) and remuxes them into `recording.mkv`.
    /// Blocking: call from a blocking thread.
    pub fn stop(mut self) -> Result<i64> {
        if self.paused {
            let _ = self.set_paused(false);
        }
        let elapsed = self.elapsed_ms();
        if let Some(v) = self.video.take() {
            v.stop(Duration::from_secs(10));
        }
        drop(self._video_fd.take());
        let Self { audio, dir, .. } = self;
        audio.stop(Duration::from_secs(10));
        finalize_files(&dir)?;
        Ok(elapsed)
    }
}

/// `video.mkv` + `audio.mka` → `recording.mkv` (stream copy).
pub fn finalize_files(dir: &Path) -> Result<()> {
    let (video, audio, out) = (dir.join("video.mkv"), dir.join("audio.mka"), dir.join("recording.mkv"));
    let nonempty = |p: &Path| p.metadata().map(|m| m.len() > 1024).unwrap_or(false);
    match (nonempty(&video), nonempty(&audio)) {
        (true, true) => {
            let status = std::process::Command::new("ffmpeg")
                .args(["-hide_banner", "-loglevel", "error", "-y", "-i"])
                .arg(&video)
                .arg("-i")
                .arg(&audio)
                .args(["-map", "0:v", "-map", "1:a", "-c", "copy"])
                .arg(&out)
                .status();
            match status {
                Ok(s) if s.success() => {
                    let _ = std::fs::remove_file(&video);
                    let _ = std::fs::remove_file(&audio);
                }
                other => tracing::warn!("no pude unir vídeo y audio ({other:?}); dejo los dos ficheros"),
            }
        }
        (false, true) => {
            let _ = std::fs::remove_file(&video);
            std::fs::rename(&audio, &out)?;
        }
        (true, false) => std::fs::rename(&video, &out)?,
        (false, false) => bail!("la grabación no tiene datos"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pipelines_parse() {
        init().unwrap();
        let dir = Path::new("/tmp/actas test \"q\"");
        let desc = audio_pipeline(dir, &Capture::default());
        assert!(desc.contains("device=\"@DEFAULT_MONITOR@\""));
        // Parsing builds elements but does not start them.
        parse(&desc).expect("audio pipeline");
        let enc = pick_encoder(VideoEncoder::X264).expect("x264");
        assert!(enc.0.starts_with("x264enc"));
        assert!(pick_encoder(VideoEncoder::Auto).is_some());
    }
}
