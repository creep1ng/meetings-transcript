//! Energy-based voice activity detection and chunking.
//!
//! Parakeet TDT handles ~4–5 minutes per pass, so each track is cut at
//! silences into chunks of at most [`MAX_CHUNK_MS`]. Silent stretches (the mic
//! while you listen) are skipped entirely, which also saves GPU time.

use crate::audio::{RATE, ms_to_sample, sample_to_ms};

const FRAME_MS: usize = 30;
/// Longest chunk sent to the ASR model. TDT accepts minutes, but long chunks
/// that mix loud and quiet speech lose whole sentences.
pub const MAX_CHUNK_MS: i64 = 30_000;
/// Regions further apart than this go to separate chunks.
const MAX_JOIN_GAP_MS: i64 = 1_500;
/// Gaps shorter than this are bridged inside a speech region.
const BRIDGE_MS: i64 = 400;
const MIN_SPEECH_MS: i64 = 250;
const PAD_MS: i64 = 200;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Region {
    pub start_ms: i64,
    pub end_ms: i64,
}

impl Region {
    pub fn len_ms(&self) -> i64 {
        self.end_ms - self.start_ms
    }
}

fn frame_db(samples: &[f32]) -> Vec<f32> {
    let n = RATE as usize * FRAME_MS / 1000;
    samples
        .chunks(n)
        .map(|f| {
            let rms = (f.iter().map(|x| x * x).sum::<f32>() / f.len() as f32).sqrt();
            20.0 * rms.max(1e-6).log10()
        })
        .collect()
}

/// Speech regions of a track, in milliseconds.
pub fn speech_regions(samples: &[f32]) -> Vec<Region> {
    let db = frame_db(samples);
    if db.is_empty() {
        return Vec::new();
    }
    let mut sorted = db.clone();
    sorted.sort_by(f32::total_cmp);
    // Noise floor from the quietest frames. If a track is almost all speech the
    // "floor" is speech too, so the threshold is capped at -35 dBFS.
    let floor = sorted[sorted.len() / 10];
    let threshold = (floor + 12.0).clamp(-50.0, -35.0);

    let mut regions: Vec<Region> = Vec::new();
    let mut start: Option<usize> = None;
    for (i, &d) in db.iter().chain(std::iter::once(&f32::NEG_INFINITY)).enumerate() {
        match (d > threshold, start) {
            (true, None) => start = Some(i),
            (false, Some(s)) => {
                let r = Region { start_ms: (s * FRAME_MS) as i64, end_ms: (i * FRAME_MS) as i64 };
                match regions.last_mut() {
                    Some(last) if r.start_ms - last.end_ms < BRIDGE_MS => last.end_ms = r.end_ms,
                    _ => regions.push(r),
                }
                start = None;
            }
            _ => {}
        }
    }
    let total = sample_to_ms(samples.len());
    regions
        .into_iter()
        .filter(|r| r.len_ms() >= MIN_SPEECH_MS)
        .map(|r| Region { start_ms: (r.start_ms - PAD_MS).max(0), end_ms: (r.end_ms + PAD_MS).min(total) })
        .collect()
}

/// Groups speech regions into ASR chunks no longer than `max_ms`, without
/// bridging pauses longer than [`MAX_JOIN_GAP_MS`]. Regions that
/// are longer than `max_ms` on their own are split at the quietest frame near
/// the limit.
pub fn chunks(samples: &[f32], regions: &[Region], max_ms: i64) -> Vec<Region> {
    let mut out: Vec<Region> = Vec::new();
    for r in regions {
        for piece in split_long(samples, *r, max_ms) {
            match out.last_mut() {
                Some(last)
                    if piece.end_ms - last.start_ms <= max_ms && piece.start_ms - last.end_ms <= MAX_JOIN_GAP_MS =>
                {
                    last.end_ms = piece.end_ms
                }
                _ => out.push(piece),
            }
        }
    }
    out
}

fn split_long(samples: &[f32], r: Region, max_ms: i64) -> Vec<Region> {
    let mut out = Vec::new();
    let mut start = r.start_ms;
    while r.end_ms - start > max_ms {
        // Look for the quietest 30 ms frame in the last fifth of the window.
        let search_from = start + max_ms * 4 / 5;
        let search_to = start + max_ms;
        let (a, b) = (ms_to_sample(search_from), ms_to_sample(search_to).min(samples.len()));
        let db = frame_db(&samples[a.min(b)..b]);
        let cut = db
            .iter()
            .enumerate()
            .min_by(|x, y| x.1.total_cmp(y.1))
            .map(|(i, _)| search_from + (i * FRAME_MS) as i64)
            .unwrap_or(search_to);
        out.push(Region { start_ms: start, end_ms: cut });
        start = cut;
    }
    out.push(Region { start_ms: start, end_ms: r.end_ms });
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signal(spec: &[(i64, bool)]) -> Vec<f32> {
        let mut out = Vec::new();
        let mut n = 0usize;
        for &(ms, loud) in spec {
            for _ in 0..ms_to_sample(ms) {
                let noise = ((n * 7919 % 1000) as f32 / 1000.0 - 0.5) * 0.002;
                out.push(if loud { (n as f32 * 0.07).sin() * 0.3 } else { noise });
                n += 1;
            }
        }
        out
    }

    #[test]
    fn finds_speech_and_bridges_short_pauses() {
        let s = signal(&[
            (2000, false),
            (1000, true),
            (200, false),
            (1000, true),
            (3000, false),
            (600, true),
            (1000, false),
        ]);
        let r = speech_regions(&s);
        assert_eq!(r.len(), 2, "{r:?}");
        assert!((r[0].start_ms - 1800).abs() <= 60 && (r[0].end_ms - 4400).abs() <= 60, "{r:?}");
        assert!(r[1].len_ms() >= 600);
    }

    #[test]
    fn silence_has_no_regions() {
        assert!(speech_regions(&signal(&[(5000, false)])).is_empty());
    }

    #[test]
    fn chunks_respect_the_limit() {
        let s = signal(&[(500, false), (25_000, true), (500, false)]);
        let regions = speech_regions(&s);
        let c = chunks(&s, &regions, 10_000);
        assert!(c.len() >= 3);
        assert!(c.iter().all(|r| r.len_ms() <= 10_000), "{c:?}");
        assert_eq!(c.first().unwrap().start_ms, regions[0].start_ms);
        assert_eq!(c.last().unwrap().end_ms, regions.last().unwrap().end_ms);
        // Several short regions are packed into one chunk.
        let s = signal(&[
            (1000, true),
            (700, false),
            (1000, true),
            (700, false),
            (1000, true),
            (3000, false),
            (1000, true),
        ]);
        let r = speech_regions(&s);
        assert_eq!(r.len(), 4);
        assert_eq!(chunks(&s, &r, MAX_CHUNK_MS).len(), 2, "the 3 s pause splits");
    }
}
