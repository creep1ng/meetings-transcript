//! Manual check of the pipeline: `transcribe <mic.wav> <system.wav> [cpu]`.
use actas_core::config::{Device, Transcription};
use actas_engine::{Engine, Job};

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt().with_env_filter(std::env::var("RUST_LOG").unwrap_or("info".into())).init();
    let args: Vec<String> = std::env::args().collect();
    let mut cfg = Transcription::default();
    if args.get(3).is_some_and(|a| a == "cpu") {
        cfg.device = Device::Cpu;
    }
    let t0 = std::time::Instant::now();
    let mut engine = Engine::load(&cfg)?;
    println!("modelos cargados en {:.1}s ({:?})", t0.elapsed().as_secs_f32(), engine.device);
    let job = Job { mic_wav: args[1].clone().into(), system_wav: args[2].clone().into(), voices: &[], threshold: 0.75 };
    let t0 = std::time::Instant::now();
    let t = engine.transcribe(&job, |p, s| eprintln!("{:>3.0}% {s}", p * 100.0))?;
    println!("transcrito en {:.1}s · {}", t0.elapsed().as_secs_f32(), t.model);
    for s in &t.segments {
        let who = s.speaker.map(|i| t.speakers[i].label.as_str()).unwrap_or("?");
        println!("[{}] {who}: {}", actas_core::fmt_ms(s.start_ms), s.text);
    }
    Ok(())
}
