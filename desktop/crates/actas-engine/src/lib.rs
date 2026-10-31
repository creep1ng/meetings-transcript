//! Local transcription pipeline:
//!
//! 1. Read the two tracks the recorder wrote: `mic.wav` (you) and
//!    `system.wav` (everyone else).
//! 2. Cut each track at silences ([`vad`]) and transcribe the chunks with
//!    Parakeet TDT 0.6B v3, keeping word timestamps.
//! 3. Diarize the system track with Sortformer (Nemotron 3 Diarization).
//! 4. Embed each system speaker with CAM++ and match against saved voices.
//! 5. Group words into segments and drop mic echo ([`assemble`]).
//!
//! Nothing here touches the network.

pub mod assemble;
pub mod audio;
mod embed;
pub mod vad;

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use actas_core::config::{Device, Transcription};
use actas_core::db::{NewSegment, NewSpeaker, NewTranscript};
use anyhow::{Context, Result, anyhow, bail};
use parakeet_rs::ort::session::Session;
use parakeet_rs::sortformer::{DiarizationConfig, Sortformer};
use parakeet_rs::{ExecutionConfig, ExecutionProvider, ParakeetTDT, TimestampMode, Transcriber};

use assemble::{Seg, Turn, Word};
use audio::{ms_to_sample, sample_to_ms};
pub use embed::Embedder;

pub const ASR_MODEL_NAME: &str = "parakeet-tdt-0.6b-v3";

/// Candidate locations of the system ONNX Runtime.
const ORT_LIBS: &[&str] =
    &["/usr/lib/libonnxruntime.so", "/usr/lib64/libonnxruntime.so", "/usr/local/lib/libonnxruntime.so"];

static ORT_ENV: OnceLock<std::result::Result<parakeet_rs::ort::environment::Environment, String>> = OnceLock::new();

/// Process-wide ONNX Runtime environment, loaded from the system library
/// (`ORT_DYLIB_PATH` overrides the search).
fn ort_env() -> Result<parakeet_rs::ort::environment::Environment> {
    ORT_ENV
        .get_or_init(|| {
            let lib = std::env::var("ORT_DYLIB_PATH")
                .ok()
                .or_else(|| ORT_LIBS.iter().find(|p| Path::new(p).exists()).map(|p| p.to_string()))
                .ok_or_else(|| {
                    "no encuentro libonnxruntime.so (instala onnxruntime o define ORT_DYLIB_PATH)".to_string()
                })?;
            parakeet_rs::ort::init_from(&lib)
                .map_err(|e| e.to_string())?
                .with_name("actas")
                .build()
                .map_err(|e| e.to_string())
        })
        .clone()
        .map_err(|e| anyhow!(e))
}

fn exec_config(cuda: bool) -> Result<ExecutionConfig> {
    let mut cfg = ExecutionConfig::new().with_environment(ort_env()?).with_intra_threads(4);
    if cuda {
        #[cfg(feature = "cuda")]
        {
            cfg = cfg.with_execution_provider(ExecutionProvider::Cuda);
        }
    } else {
        cfg = cfg.with_execution_provider(ExecutionProvider::Cpu);
    }
    Ok(cfg)
}

pub(crate) fn ort_session(model: &Path, cuda: bool) -> Result<Session> {
    exec_config(cuda)?.build_session(model).with_context(|| format!("cargando {}", model.display()))
}

/// Model files that are not on disk yet.
pub fn missing_models(cfg: &Transcription) -> Vec<String> {
    let mut missing = Vec::new();
    for f in ["encoder-model.onnx", "encoder-model.onnx.data", "decoder_joint-model.onnx", "vocab.txt"] {
        if !cfg.asr_model.join(f).exists() {
            missing.push(cfg.asr_model.join(f).display().to_string());
        }
    }
    for p in [&cfg.diarization_model, &cfg.embedding_model] {
        if !p.exists() {
            missing.push(p.display().to_string());
        }
    }
    missing
}

/// Inputs of one transcription job.
pub struct Job<'a> {
    pub mic_wav: PathBuf,
    pub system_wav: PathBuf,
    /// Saved voices `(id, name, embedding)`.
    pub voices: &'a [(i64, String, Vec<f32>)],
    pub threshold: f32,
}

/// Loaded models. Loading takes a few seconds and ~3 GB of VRAM, so the
/// daemon keeps one `Engine` while jobs are pending and drops it when idle.
pub struct Engine {
    asr: ParakeetTDT,
    diar: Sortformer,
    embedder: Embedder,
    pub device: Device,
}

impl Engine {
    pub fn load(cfg: &Transcription) -> Result<Self> {
        let missing = missing_models(cfg);
        if !missing.is_empty() {
            bail!("faltan modelos: {}", missing.join(", "));
        }
        let try_load = |cuda: bool| -> Result<Self> {
            let asr = ParakeetTDT::from_pretrained(&cfg.asr_model, Some(exec_config(cuda)?))
                .map_err(|e| anyhow!("Parakeet: {e}"))?;
            let diar =
                Sortformer::with_config(&cfg.diarization_model, Some(exec_config(cuda)?), DiarizationConfig::default())
                    .map_err(|e| anyhow!("Sortformer: {e}"))?;
            let embedder = Embedder::load(&cfg.embedding_model, cuda)?;
            Ok(Self { asr, diar, embedder, device: if cuda { Device::Cuda } else { Device::Cpu } })
        };
        match cfg.device {
            Device::Cuda => try_load(true).or_else(|e| {
                tracing::warn!("CUDA no disponible ({e:#}); uso la CPU");
                try_load(false)
            }),
            Device::Cpu => try_load(false),
        }
    }

    /// Transcribes one track; returns words with absolute times.
    fn transcribe_track(&mut self, samples: &[f32], mut progress: impl FnMut(f32)) -> Result<Vec<Word>> {
        let regions = vad::speech_regions(samples);
        let chunks = vad::chunks(samples, &regions, vad::MAX_CHUNK_MS);
        let total: i64 = chunks.iter().map(vad::Region::len_ms).sum::<i64>().max(1);
        let mut done = 0;
        let mut words = Vec::new();
        for c in &chunks {
            let (a, b) = (ms_to_sample(c.start_ms), ms_to_sample(c.end_ms).min(samples.len()));
            if b <= a {
                continue;
            }
            let result = self
                .asr
                .transcribe_samples(samples[a..b].to_vec(), audio::RATE, 1, Some(TimestampMode::Words))
                .map_err(|e| anyhow!("Parakeet: {e}"))?;
            for t in result.tokens {
                words.push(Word {
                    text: t.text,
                    start_ms: c.start_ms + (t.start * 1000.0) as i64,
                    end_ms: c.start_ms + (t.end * 1000.0) as i64,
                    speaker: None,
                });
            }
            done += c.len_ms();
            progress(done as f32 / total as f32);
        }
        Ok(words)
    }

    fn diarize(&mut self, samples: &[f32]) -> Result<Vec<Turn>> {
        self.diar.reset_state();
        let segs = self.diar.diarize(samples.to_vec(), audio::RATE, 1).map_err(|e| anyhow!("Sortformer: {e}"))?;
        Ok(segs
            .into_iter()
            .map(|s| Turn {
                speaker: s.speaker_id,
                start_ms: sample_to_ms(s.start as usize),
                end_ms: sample_to_ms(s.end as usize),
            })
            .collect())
    }

    /// Clips where only `speaker` talks, longest first, at most ~40 s in total.
    fn solo_clips<'s>(samples: &'s [f32], turns: &[Turn], speaker: usize) -> Vec<&'s [f32]> {
        let mut solo: Vec<(i64, i64)> = turns
            .iter()
            .filter(|t| t.speaker == speaker)
            .map(|t| {
                // Trim the parts that overlap other speakers.
                let (mut s, mut e) = (t.start_ms, t.end_ms);
                for o in turns.iter().filter(|o| o.speaker != speaker) {
                    if o.start_ms <= s && o.end_ms > s {
                        s = o.end_ms;
                    }
                    if o.start_ms < e && o.end_ms >= e {
                        e = o.start_ms;
                    }
                }
                (s, e)
            })
            .filter(|(s, e)| e - s >= embed::MIN_CLIP_MS)
            .collect();
        solo.sort_by_key(|(s, e)| -(e - s));
        let mut budget = 40_000;
        let mut out = Vec::new();
        for (s, e) in solo {
            if budget <= 0 {
                break;
            }
            let e = e.min(s + 10_000);
            budget -= e - s;
            let (a, b) = (ms_to_sample(s), ms_to_sample(e).min(samples.len()));
            if b > a {
                out.push(&samples[a..b]);
            }
        }
        out
    }

    /// Runs the whole pipeline. `progress(fraction, stage)` is called often.
    pub fn transcribe(&mut self, job: &Job, mut progress: impl FnMut(f32, &str)) -> Result<NewTranscript> {
        progress(0.02, "Leyendo audio");
        let mic = audio::read_wav(&job.mic_wav).unwrap_or_else(|e| {
            tracing::warn!("sin pista de micrófono: {e:#}");
            Vec::new()
        });
        let system = audio::read_wav(&job.system_wav).unwrap_or_else(|e| {
            tracing::warn!("sin pista de sistema: {e:#}");
            Vec::new()
        });
        if mic.is_empty() && system.is_empty() {
            bail!("la grabación no tiene audio");
        }

        let mut mic = mic;
        assemble::suppress_echo(&mut mic, &system);

        progress(0.05, "Transcribiendo tu micrófono");
        let mic_words = self.transcribe_track(&mic, |f| progress(0.05 + 0.25 * f, "Transcribiendo tu micrófono"))?;

        progress(0.30, "Separando hablantes");
        let turns = if system.is_empty() { Vec::new() } else { self.diarize(&system)? };

        progress(0.40, "Transcribiendo a los demás");
        let mut sys_words =
            self.transcribe_track(&system, |f| progress(0.40 + 0.45 * f, "Transcribiendo a los demás"))?;
        assemble::assign_speakers(&mut sys_words, &turns);

        // Speakers that actually said something, in order of appearance.
        let mut order: Vec<usize> = Vec::new();
        for w in &sys_words {
            if let Some(s) = w.speaker
                && !order.contains(&s)
            {
                order.push(s);
            }
        }

        progress(0.88, "Reconociendo voces");
        let mut speakers = vec![NewSpeaker {
            track: "mic".into(),
            label: "Yo".into(),
            name: None,
            voice_id: None,
            similarity: None,
            embedding: None,
        }];
        let mut taken: Vec<i64> = Vec::new();
        let mut index_of = std::collections::HashMap::new();
        let mut unknown_speaker = None;
        for (n, &spk) in order.iter().enumerate() {
            let clips = Self::solo_clips(&system, &turns, spk);
            let embedding = match self.embedder.embed_many(&clips) {
                Ok(e) => e,
                Err(e) => {
                    tracing::warn!("no pude calcular la huella del hablante {spk}: {e:#}");
                    None
                }
            };
            let matched = embedding.as_deref().and_then(|e| {
                let free: Vec<_> = job.voices.iter().filter(|v| !taken.contains(&v.0)).cloned().collect();
                actas_core::voice::best_match(e, &free, job.threshold)
                    .map(|(id, name, sim)| (id, name.to_string(), sim))
            });
            if let Some((id, ..)) = &matched {
                taken.push(*id);
            }
            index_of.insert(spk, speakers.len());
            speakers.push(NewSpeaker {
                track: "system".into(),
                label: format!("Hablante {}", n + 1),
                name: matched.as_ref().map(|m| m.1.clone()),
                voice_id: matched.as_ref().map(|m| m.0),
                similarity: matched.as_ref().map(|m| m.2),
                embedding,
            });
        }
        // Words the diarizer could not place.
        if sys_words.iter().any(|w| w.speaker.is_none()) {
            unknown_speaker = Some(speakers.len());
            speakers.push(NewSpeaker {
                track: "system".into(),
                label: "Otros".into(),
                name: None,
                voice_id: None,
                similarity: None,
                embedding: None,
            });
        }
        for w in &mut sys_words {
            w.speaker = match w.speaker {
                Some(s) => index_of.get(&s).copied(),
                None => unknown_speaker,
            };
        }

        progress(0.95, "Montando la transcripción");
        let sys_segs = assemble::group(&sys_words, 2000, 1500);
        if std::env::var_os("ACTAS_DEBUG_WORDS").is_some() {
            for w in &mic_words {
                tracing::debug!(target: "actas_engine::words", "mic {:>6}-{:>6} {}", w.start_ms, w.end_ms, w.text);
            }
            for w in &sys_words {
                tracing::debug!(target: "actas_engine::words", "sys {:>6}-{:>6} {:?} {}", w.start_ms, w.end_ms, w.speaker, w.text);
            }
        }
        // Echo is filtered per word (a segment can mix your words and leaked ones).
        let mic_words: Vec<Word> = mic_words
            .into_iter()
            .filter(|w| {
                let span = Seg { speaker: None, start_ms: w.start_ms - 80, end_ms: w.end_ms + 80, text: String::new() };
                !assemble::is_echo_by_level(&span, &mic, &system)
            })
            .map(|w| Word { speaker: Some(0), ..w })
            .collect();
        let mic_segs = assemble::drop_echo(assemble::group(&mic_words, 2000, 1500), &sys_segs);

        let mut all: Vec<Seg> = mic_segs.into_iter().chain(sys_segs).collect();
        all.sort_by_key(|s| s.start_ms);
        // Keep "Yo" even if you stayed silent; drop other speakers without words.
        let used: Vec<bool> = (0..speakers.len()).map(|i| i == 0 || all.iter().any(|s| s.speaker == Some(i))).collect();
        let remap: Vec<Option<usize>> = used
            .iter()
            .scan(0usize, |next, &u| {
                Some(if u {
                    *next += 1;
                    Some(*next - 1)
                } else {
                    None
                })
            })
            .collect();
        let speakers: Vec<NewSpeaker> = speakers.into_iter().zip(&used).filter(|(_, u)| **u).map(|(s, _)| s).collect();
        let segments = all
            .into_iter()
            .map(|s| NewSegment {
                speaker: s.speaker.and_then(|i| remap[i]),
                start_ms: s.start_ms,
                end_ms: s.end_ms,
                text: s.text,
            })
            .collect();

        Ok(NewTranscript { model: format!("{ASR_MODEL_NAME} · {:?}", self.device).to_lowercase(), speakers, segments })
    }
}
