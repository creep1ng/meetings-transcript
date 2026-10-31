//! Voice embeddings: storage format and matching.

/// Little-endian f32 bytes.
pub fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

pub fn from_blob(b: &[u8]) -> Vec<f32> {
    b.as_chunks::<4>().0.iter().map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

pub fn normalized(v: &[f32]) -> Vec<f32> {
    let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm <= f32::EPSILON {
        return v.to_vec();
    }
    v.iter().map(|x| x / norm).collect()
}

pub fn cosine(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let (a, b) = (normalized(a), normalized(b));
    a.iter().zip(&b).map(|(x, y)| x * y).sum()
}

/// Running mean of `n` normalized samples plus one more, renormalized.
pub fn merge(old: &[f32], n: i64, new: &[f32]) -> Vec<f32> {
    if old.len() != new.len() {
        return normalized(new);
    }
    let n = n.max(1) as f32;
    let new = normalized(new);
    let mean: Vec<f32> = old.iter().zip(&new).map(|(o, x)| (o * n + x) / (n + 1.0)).collect();
    normalized(&mean)
}

/// Best voice at or above `threshold`: `(voice_id, name, similarity)`.
pub fn best_match<'a>(
    emb: &[f32],
    voices: &'a [(i64, String, Vec<f32>)],
    threshold: f32,
) -> Option<(i64, &'a str, f32)> {
    voices
        .iter()
        .map(|(id, name, v)| (*id, name.as_str(), cosine(emb, v)))
        .filter(|(_, _, s)| *s >= threshold)
        .max_by(|a, b| a.2.total_cmp(&b.2))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blob_round_trip() {
        let v = vec![0.5, -1.25, 3.0];
        assert_eq!(from_blob(&to_blob(&v)), v);
    }

    #[test]
    fn matching_picks_closest_above_threshold() {
        let voices = vec![(1, "Laura".to_string(), vec![1.0, 0.0]), (2, "Marcos".to_string(), vec![0.0, 1.0])];
        let m = best_match(&[0.9, 0.1], &voices, 0.75).unwrap();
        assert_eq!((m.0, m.1), (1, "Laura"));
        assert!(best_match(&[0.7, 0.7], &voices, 0.75).is_none());
    }

    #[test]
    fn merge_moves_towards_new_sample() {
        let m = merge(&[1.0, 0.0], 1, &[0.0, 1.0]);
        assert!((m[0] - m[1]).abs() < 1e-6);
        assert!((m.iter().map(|x| x * x).sum::<f32>() - 1.0).abs() < 1e-5);
    }
}
