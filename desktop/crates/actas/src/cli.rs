//! Command-line interface: daemon control (used by the Noctalia widget and
//! scripts), model download and exports.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use actas_core::config::Config;
use actas_core::export::{Format, Transcript};
use actas_core::ipc::{self, Request, Response};
use actas_core::{Db, fmt_ms};
use anyhow::{Context, Result, bail};

pub const USAGE: &str = "\
actas — grabación y transcripción local de reuniones

Uso:
  actas                      Abre la ventana principal
  actas popover              Abre/cierra el panel de grabación
  actas status [--json]      Estado del demonio
  actas start [--audio] [--title T]
  actas stop | toggle | pause
  actas mark [ETIQUETA]
  actas transcribe ID        Vuelve a transcribir una reunión
  actas list                 Lista las reuniones
  actas export ID [md|txt|srt|json]
  actas models               Estado de los modelos
  actas models download      Descarga los modelos que falten (única operación con red)
";

fn send(req: Request) -> Result<()> {
    ipc::call(&req)?;
    Ok(())
}

pub fn run(args: &[String]) -> Result<()> {
    let arg = |i: usize| args.get(i).map(String::as_str);
    match arg(0) {
        Some("status") => status(args.iter().any(|a| a == "--json")),
        Some("start") => {
            let video = !args.iter().any(|a| a == "--audio");
            let title = args.iter().position(|a| a == "--title").and_then(|i| args.get(i + 1)).cloned();
            send(Request::Start { title, video })
        }
        Some("stop") => send(Request::Stop),
        Some("toggle") => send(Request::Toggle),
        Some("pause") => send(Request::TogglePause),
        Some("mark") => send(Request::Mark { label: (args.len() > 1).then(|| args[1..].join(" ")) }),
        Some("transcribe") => send(Request::Transcribe { meeting_id: arg(1).context("falta el ID")?.into() }),
        Some("list") => list(),
        Some("export") => export(arg(1).context("falta el ID")?, arg(2).unwrap_or("md")),
        Some("models") if arg(1) == Some("download") => download_models(args.iter().any(|a| a == "--yes")),
        Some("models") => models_status(),
        Some("help" | "--help" | "-h") => {
            print!("{USAGE}");
            Ok(())
        }
        Some(other) => bail!("orden desconocida «{other}»\n\n{USAGE}"),
        None => unreachable!(),
    }
}

/// `--json` prints one line meant for bar widgets:
/// `{"state": "recording", "text": "12:41", "tooltip": "...", "status": {...}}`.
fn status(json: bool) -> Result<()> {
    let status = match ipc::request(&Request::Status) {
        Ok(Response::Status(s)) => Some(s),
        Ok(_) => None,
        Err(e) if json => {
            println!("{}", serde_json::json!({ "state": "offline", "text": "", "tooltip": format!("Actas: {e}") }));
            return Ok(());
        }
        Err(e) => return Err(e),
    };
    let s = status.context("respuesta inesperada")?;
    let (text, tooltip) = match (&s.recording, &s.suggestion, &s.transcribing) {
        (Some(r), ..) => {
            (fmt_ms(r.elapsed_ms), format!("{} «{}»", if r.paused { "En pausa" } else { "Grabando" }, r.title))
        }
        (None, Some(sg), _) => (
            "¿grabar?".into(),
            format!(
                "Llamada detectada ({}){}",
                sg.app,
                sg.title.as_ref().map(|t| format!(" · «{t}»")).unwrap_or_default()
            ),
        ),
        (None, None, Some(j)) => {
            (format!("{:.0} %", j.progress * 100.0), format!("Transcribiendo «{}» · {}", j.title, j.stage))
        }
        _ => (String::new(), "Actas · en espera".into()),
    };
    if json {
        println!("{}", serde_json::json!({ "state": s.state_name(), "text": text, "tooltip": tooltip, "status": s }));
    } else {
        println!("{tooltip}{}", if text.is_empty() { String::new() } else { format!(" · {text}") });
        if s.queued > 0 {
            println!("{} reunión(es) en cola", s.queued);
        }
        if !s.missing_models.is_empty() {
            println!("Faltan modelos: ejecuta «actas models download»");
        }
    }
    Ok(())
}

fn list() -> Result<()> {
    let db = Db::open_default()?;
    for m in db.meetings()? {
        println!("{}  {:<12} {:>8}  {}", m.id, m.status.as_str(), fmt_ms(m.duration_ms), m.title);
    }
    Ok(())
}

fn export(id: &str, fmt: &str) -> Result<()> {
    let format = Format::ALL
        .into_iter()
        .find(|f| f.extension() == fmt)
        .with_context(|| format!("formato desconocido «{fmt}» (md, txt, srt, json)"))?;
    let db = Db::open_default()?;
    let m = db.meeting(id)?.context("esa reunión no existe")?;
    let (speakers, segments, marks) = (db.speakers(id)?, db.segments(id)?, db.marks(id)?);
    let t = Transcript { meeting: &m, speakers: &speakers, segments: &segments, marks: &marks };
    print!("{}", t.render(format));
    Ok(())
}

// ---------- models ----------

pub struct ModelFile {
    pub url: &'static str,
    pub dest: PathBuf,
    pub approx_mb: u64,
}

macro_rules! hf {
    () => {
        "https://huggingface.co/altunenes/parakeet-rs/resolve/main"
    };
}

pub fn model_files(cfg: &Config) -> Vec<ModelFile> {
    let t = &cfg.transcription;
    let tdt = |name: &str| t.asr_model.join(name);
    vec![
        ModelFile { url: concat!(hf!(), "/parakeet-tdt-0.6b-v3/vocab.txt"), dest: tdt("vocab.txt"), approx_mb: 1 },
        ModelFile {
            url: concat!(hf!(), "/parakeet-tdt-0.6b-v3/encoder-model.onnx"),
            dest: tdt("encoder-model.onnx"),
            approx_mb: 4,
        },
        ModelFile {
            url: concat!(hf!(), "/parakeet-tdt-0.6b-v3/decoder_joint-model.onnx"),
            dest: tdt("decoder_joint-model.onnx"),
            approx_mb: 73,
        },
        ModelFile {
            url: concat!(hf!(), "/parakeet-tdt-0.6b-v3/encoder-model.onnx.data"),
            dest: tdt("encoder-model.onnx.data"),
            approx_mb: 2477,
        },
        ModelFile {
            url: concat!(hf!(), "/nemotron-3-diarization/nemotron3_diar_v3.onnx"),
            dest: t.diarization_model.clone(),
            approx_mb: 403,
        },
        ModelFile {
            url: "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx",
            dest: t.embedding_model.clone(),
            approx_mb: 30,
        },
    ]
}

pub fn missing_files(cfg: &Config) -> Vec<ModelFile> {
    model_files(cfg).into_iter().filter(|f| !f.dest.exists()).collect()
}

fn models_status() -> Result<()> {
    let cfg = Config::load_or_default();
    for f in model_files(&cfg) {
        println!("{} {}", if f.dest.exists() { "✓" } else { "✗" }, f.dest.display());
    }
    Ok(())
}

/// Downloads `url` to `dest` through a `.part` file, reporting progress (0..1).
pub fn download(url: &str, dest: &Path, mut progress: impl FnMut(u64, Option<u64>)) -> Result<()> {
    if let Some(dir) = dest.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let part = dest.with_extension(format!(
        "{}part",
        dest.extension().map(|e| format!("{}.", e.to_string_lossy())).unwrap_or_default()
    ));
    let resp = ureq::get(url).call().with_context(|| format!("descargando {url}"))?;
    let total = resp.headers().get("content-length").and_then(|v| v.to_str().ok()).and_then(|v| v.parse().ok());
    let mut reader = resp.into_body().into_reader();
    let mut out = std::fs::File::create(&part)?;
    let mut buf = vec![0u8; 1 << 20];
    let mut done = 0u64;
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        out.write_all(&buf[..n])?;
        done += n as u64;
        progress(done, total);
    }
    out.sync_all()?;
    if let Some(t) = total
        && done != t
    {
        bail!("descarga incompleta de {url} ({done} de {t} bytes)");
    }
    std::fs::rename(&part, dest)?;
    Ok(())
}

fn download_models(yes: bool) -> Result<()> {
    let cfg = Config::load_or_default();
    let missing = missing_files(&cfg);
    if missing.is_empty() {
        println!("Todos los modelos están descargados.");
        return Ok(());
    }
    let total: u64 = missing.iter().map(|f| f.approx_mb).sum();
    println!("Se descargarán {} fichero(s), unos {total} MB:", missing.len());
    for f in &missing {
        println!("  {}  ←  {}", f.dest.display(), f.url);
    }
    if !yes {
        print!("Esta es la única operación de Actas que usa la red. ¿Continuar? [s/N] ");
        std::io::stdout().flush()?;
        let mut answer = String::new();
        std::io::stdin().read_line(&mut answer)?;
        if !matches!(answer.trim().to_lowercase().as_str(), "s" | "si" | "sí" | "y" | "yes") {
            println!("Cancelado.");
            return Ok(());
        }
    }
    for f in &missing {
        let name = f.dest.file_name().unwrap_or_default().to_string_lossy().to_string();
        let mut last = 0u64;
        download(f.url, &f.dest, |done, total| {
            if done - last >= 8 << 20 || Some(done) == total {
                last = done;
                match total {
                    Some(t) => eprint!("\r{name}: {:.0} %   ", done as f64 * 100.0 / t as f64),
                    None => eprint!("\r{name}: {} MB   ", done >> 20),
                }
            }
        })?;
        eprintln!("\r{name}: listo         ");
    }
    // Let the daemon pick up queued work.
    let _ = ipc::request(&Request::ReloadConfig);
    Ok(())
}
