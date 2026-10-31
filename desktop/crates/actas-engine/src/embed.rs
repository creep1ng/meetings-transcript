//! Speaker embeddings with WeSpeaker CAM++ (Kaldi fbank features → 512-d vector).

use std::path::Path;

use anyhow::{Context, Result, anyhow};
use parakeet_rs::ort::session::Session;
use parakeet_rs::ort::value::Tensor;

use crate::ort_session;

pub struct Embedder {
    session: Session,
}

/// Shortest clip worth embedding.
pub const MIN_CLIP_MS: i64 = 1000;

impl Embedder {
    pub fn load(model: &Path, cuda: bool) -> Result<Self> {
        Ok(Self { session: ort_session(model, cuda)? })
    }

    /// Embedding of one 16 kHz mono clip.
    pub fn embed(&mut self, clip: &[f32]) -> Result<Vec<f32>> {
        let feats = knf_rs::compute_fbank(clip).map_err(|e| anyhow!("fbank: {e}"))?;
        let (frames, bins) = feats.dim();
        let data: Vec<f32> = feats.iter().copied().collect();
        let input = Tensor::from_array(([1usize, frames, bins], data))?;
        let outputs = self.session.run(parakeet_rs::ort::inputs!["feats" => input])?;
        let (_, emb) = outputs["embs"].try_extract_tensor::<f32>().context("salida embs")?;
        Ok(emb.to_vec())
    }

    /// Mean of the normalized embeddings of several clips.
    pub fn embed_many(&mut self, clips: &[&[f32]]) -> Result<Option<Vec<f32>>> {
        let mut sum: Option<Vec<f32>> = None;
        for clip in clips {
            let e = actas_core::voice::normalized(&self.embed(clip)?);
            match &mut sum {
                Some(s) => s.iter_mut().zip(&e).for_each(|(a, b)| *a += b),
                None => sum = Some(e),
            }
        }
        Ok(sum.map(|s| actas_core::voice::normalized(&s)))
    }
}
