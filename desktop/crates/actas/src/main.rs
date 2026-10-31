//! `actas`: main window, recording panel and command line.

mod cli;
mod external;
mod model;
mod player;
mod ui;

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "warn".into()))
        .with_writer(std::io::stderr)
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        None => ui::run(Some(ui::Open::Main)),
        Some("popover") => ui::run(Some(ui::Open::Popover)),
        Some("--background") => ui::run(None),
        Some(_) => cli::run(&args),
    };
    if let Err(e) = result {
        eprintln!("actas: {e:#}");
        std::process::exit(1);
    }
}
