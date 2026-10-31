//! Turning timed words into transcript segments: speaker assignment, sentence
//! grouping and removal of mic echo.

/// A word with absolute times (ms) and an optional speaker index.
#[derive(Debug, Clone, PartialEq)]
pub struct Word {
    pub text: String,
    pub start_ms: i64,
    pub end_ms: i64,
    pub speaker: Option<usize>,
}

/// A diarization turn: `speaker` talks between `start_ms` and `end_ms`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Turn {
    pub speaker: usize,
    pub start_ms: i64,
    pub end_ms: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Seg {
    pub speaker: Option<usize>,
    pub start_ms: i64,
    pub end_ms: i64,
    pub text: String,
}

/// Gives each word the speaker with the most overlap; words in a gap of the
/// diarization take the nearest turn (within 1 s).
pub fn assign_speakers(words: &mut [Word], turns: &[Turn]) {
    for w in words.iter_mut() {
        let best = turns
            .iter()
            .map(|t| (t.speaker, t.end_ms.min(w.end_ms) - t.start_ms.max(w.start_ms)))
            .filter(|(_, o)| *o > 0)
            .max_by_key(|(_, o)| *o)
            .map(|(s, _)| s);
        w.speaker = best.or_else(|| {
            let mid = (w.start_ms + w.end_ms) / 2;
            turns
                .iter()
                .map(|t| (t.speaker, (t.start_ms - mid).abs().min((t.end_ms - mid).abs())))
                .filter(|(_, d)| *d <= 1000)
                .min_by_key(|(_, d)| *d)
                .map(|(s, _)| s)
        });
    }
}

fn is_punct(word: &str) -> bool {
    !word.is_empty() && word.chars().all(|c| matches!(c, '.' | ',' | '!' | '?' | ';' | ':' | ')' | '…'))
}

fn ends_sentence(word: &str) -> bool {
    word.ends_with(['.', '?', '!', '…'])
}

/// Groups words into segments. A segment ends when the speaker changes, at a
/// pause longer than `max_gap_ms`, or at the end of a sentence once it is at
/// least `min_len_ms` long.
pub fn group(words: &[Word], max_gap_ms: i64, min_len_ms: i64) -> Vec<Seg> {
    let mut out = group_raw(words, max_gap_ms, min_len_ms);
    // A segment made only of punctuation is an artefact of dropped words.
    out.retain(|s| s.text.chars().any(char::is_alphanumeric));
    for s in &mut out {
        s.text = s.text.trim_start_matches(|c: char| !c.is_alphanumeric() && c != '¿' && c != '¡').to_string();
    }
    out
}

fn group_raw(words: &[Word], max_gap_ms: i64, min_len_ms: i64) -> Vec<Seg> {
    let mut out: Vec<Seg> = Vec::new();
    let mut open = false;
    for w in words {
        let text = w.text.trim();
        if text.is_empty() {
            continue;
        }
        if open {
            let cur = out.last_mut().unwrap();
            let same = cur.speaker == w.speaker && w.start_ms - cur.end_ms <= max_gap_ms;
            if same || is_punct(text) {
                if !is_punct(text) {
                    cur.text.push(' ');
                }
                cur.text.push_str(text);
                cur.end_ms = cur.end_ms.max(w.end_ms);
                if ends_sentence(text) && cur.end_ms - cur.start_ms >= min_len_ms {
                    open = false;
                }
                continue;
            }
        }
        out.push(Seg { speaker: w.speaker, start_ms: w.start_ms, end_ms: w.end_ms, text: text.to_string() });
        open = !(ends_sentence(text) && w.end_ms - w.start_ms >= min_len_ms);
    }
    out
}

fn tokens(text: &str) -> Vec<String> {
    text.split_whitespace()
        .map(|w| w.chars().filter(|c| c.is_alphanumeric()).collect::<String>().to_lowercase())
        .filter(|w| !w.is_empty())
        .collect()
}

/// Share of the words of `a` that also appear in `b`.
fn containment(a: &str, b: &str) -> f32 {
    let (a, b) = (tokens(a), tokens(b));
    if a.is_empty() {
        return 0.0;
    }
    a.iter().filter(|w| b.contains(w)).count() as f32 / a.len() as f32
}

fn rms_db(x: &[f32]) -> f32 {
    if x.is_empty() {
        return -120.0;
    }
    20.0 * (x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32).sqrt().max(1e-6).log10()
}

/// True if, during `seg`, the system track is clearly louder than the mic: the
/// mic is only hearing the speakers. Your own voice reaches the mic much
/// louder than the leak from the speakers.
pub fn is_echo_by_level(seg: &Seg, mic: &[f32], system: &[f32]) -> bool {
    use crate::audio::ms_to_sample;
    let (a, b) = (ms_to_sample(seg.start_ms), ms_to_sample(seg.end_ms));
    let slice = |x: &[f32]| -> f32 { rms_db(&x[a.min(x.len())..b.min(x.len())]) };
    echo_levels(slice(mic), slice(system))
}

/// The leak from the speakers usually arrives 20 dB or more below the
/// original; your own voice is rarely 10 dB quieter than the others'.
fn echo_levels(mic_db: f32, system_db: f32) -> bool {
    system_db > -45.0 && mic_db < system_db - 10.0
}

/// Mutes the 30 ms mic frames that only carry the leak from the speakers, so
/// the ASR model never sees the echo. Parakeet tends to drop the real speech
/// that follows a long stretch of quiet echo in the same chunk.
pub fn suppress_echo(mic: &mut [f32], system: &[f32]) {
    let n = crate::audio::RATE as usize * 30 / 1000;
    for (i, frame) in mic.chunks_mut(n).enumerate() {
        let sys = &system[(i * n).min(system.len())..((i + 1) * n).min(system.len())];
        if echo_levels(rms_db(frame), rms_db(sys)) {
            frame.fill(0.0);
        }
    }
}

/// Without headphones the mic also picks up the other participants. Drops mic
/// segments whose words mostly repeat what the system track said at the same
/// time (±1.5 s).
pub fn drop_echo(mic: Vec<Seg>, system: &[Seg]) -> Vec<Seg> {
    mic.into_iter()
        .filter(|m| {
            let around: String = system
                .iter()
                .filter(|s| s.start_ms <= m.end_ms + 1500 && s.end_ms + 1500 >= m.start_ms)
                .map(|s| s.text.as_str())
                .collect::<Vec<_>>()
                .join(" ");
            containment(&m.text, &around) < 0.6
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(text: &str, start_ms: i64, end_ms: i64) -> Word {
        Word { text: text.into(), start_ms, end_ms, speaker: None }
    }

    #[test]
    fn assigns_by_overlap_and_nearest() {
        let turns = [Turn { speaker: 0, start_ms: 0, end_ms: 1000 }, Turn { speaker: 1, start_ms: 1200, end_ms: 3000 }];
        let mut words = vec![
            w("hola", 100, 400),
            w("qué", 850, 1250),
            w("tal", 1500, 1800),
            w("fin", 3500, 3700),
            w("lejos", 9000, 9100),
        ];
        assign_speakers(&mut words, &turns);
        let s: Vec<_> = words.iter().map(|w| w.speaker).collect();
        assert_eq!(s, vec![Some(0), Some(0), Some(1), Some(1), None]);
    }

    #[test]
    fn groups_by_speaker_sentence_and_gap() {
        let mut words = vec![
            w("Hola", 0, 300),
            w("a", 300, 400),
            w("todos", 400, 900),
            w(".", 900, 900),
            w("Empezamos", 1000, 1500),
            w("ya", 1500, 2600),
            w("Vale", 2700, 3000),
            w("luego", 9000, 9500),
        ];
        for x in &mut words[..6] {
            x.speaker = Some(0);
        }
        for x in &mut words[6..] {
            x.speaker = Some(1);
        }
        let g = group(&words, 2000, 800);
        let texts: Vec<_> = g.iter().map(|s| s.text.as_str()).collect();
        assert_eq!(texts, vec!["Hola a todos.", "Empezamos ya", "Vale", "luego"]);
        assert_eq!((g[0].start_ms, g[0].end_ms), (0, 900));
    }

    #[test]
    fn punctuation_only_segments_are_dropped() {
        let words = vec![w(",", 0, 0), w(".", 10, 10), w("Hola", 5000, 5300), w(".", 5300, 5300)];
        let g = group(&words, 2000, 0);
        assert_eq!(g.len(), 1);
        assert_eq!(g[0].text, "Hola.");
        let g = group(&[w(".,", 0, 10), w("¿Vale?", 20, 900)], 2000, 0);
        assert_eq!(g.last().unwrap().text, "¿Vale?");
    }

    #[test]
    fn short_sentences_are_merged() {
        let words = vec![w("Sí.", 0, 200), w("Claro.", 300, 500), w("Lo", 600, 700), w("veo.", 700, 1200)];
        let g = group(&words, 2000, 800);
        assert_eq!(g.len(), 1);
        assert_eq!(g[0].text, "Sí. Claro. Lo veo.");
    }

    #[test]
    fn echo_by_level() {
        let seg = Seg { speaker: None, start_ms: 0, end_ms: 1000, text: String::new() };
        let tone: Vec<f32> = (0..16_000).map(|i| (i as f32 * 0.05).sin() * 0.3).collect();
        let quiet: Vec<f32> = tone.iter().map(|x| x * 0.15).collect();
        assert!(is_echo_by_level(&seg, &quiet, &tone), "leak from the speakers");
        let mut mic = quiet.clone();
        suppress_echo(&mut mic, &tone);
        assert!(mic.iter().all(|x| *x == 0.0));
        let mut mic = tone.clone();
        suppress_echo(&mut mic, &tone);
        assert_eq!(mic, tone);
        assert!(!is_echo_by_level(&seg, &tone, &tone), "double talk at similar level");
        assert!(!is_echo_by_level(&seg, &tone, &vec![0.0; 16_000]), "others silent");
    }

    #[test]
    fn echo_is_removed() {
        let system =
            vec![Seg { speaker: Some(0), start_ms: 1000, end_ms: 4000, text: "El despliegue pasa al jueves".into() }];
        let mic = vec![
            Seg { speaker: None, start_ms: 1100, end_ms: 3900, text: "el despliegue pasa jueves".into() },
            Seg { speaker: None, start_ms: 5000, end_ms: 7000, text: "Necesito staging antes".into() },
        ];
        let kept = drop_echo(mic, &system);
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].text, "Necesito staging antes");
    }
}
