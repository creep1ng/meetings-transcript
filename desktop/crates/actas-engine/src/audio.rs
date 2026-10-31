//! Reading the 16 kHz mono WAV tracks written by the recorder.

use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use anyhow::{Context, Result, bail};

pub const RATE: u32 = 16_000;

/// Reads a WAV as mono f32 at 16 kHz. Tolerates a header whose data size was
/// never finalised (recording interrupted by a crash): the data then runs to
/// the end of the file.
pub fn read_wav(path: &Path) -> Result<Vec<f32>> {
    match hound::WavReader::open(path) {
        Ok(reader) if reader.duration() > 0 => from_reader(reader),
        _ => read_raw_pcm(path).with_context(|| format!("leyendo {}", path.display())),
    }
}

fn from_reader<R: Read>(reader: hound::WavReader<R>) -> Result<Vec<f32>> {
    let spec = reader.spec();
    if spec.sample_rate != RATE {
        bail!("se esperaba audio a {RATE} Hz y llegó a {} Hz", spec.sample_rate);
    }
    let samples: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader.into_samples::<f32>().collect::<Result<_, _>>()?,
        hound::SampleFormat::Int => {
            reader.into_samples::<i16>().map(|s| s.map(|s| s as f32 / 32768.0)).collect::<Result<_, _>>()?
        }
    };
    Ok(downmix(samples, spec.channels))
}

fn downmix(samples: Vec<f32>, channels: u16) -> Vec<f32> {
    if channels <= 1 {
        return samples;
    }
    let c = channels as usize;
    samples.chunks_exact(c).map(|f| f.iter().sum::<f32>() / c as f32).collect()
}

/// Fallback for truncated files: find the `data` chunk and read S16LE mono to EOF.
fn read_raw_pcm(path: &Path) -> Result<Vec<f32>> {
    let mut f = std::fs::File::open(path)?;
    let mut head = vec![0u8; 4096.min(f.metadata()?.len() as usize)];
    f.read_exact(&mut head)?;
    let Some(pos) = head.windows(4).position(|w| w == b"data") else {
        bail!("no es un WAV válido");
    };
    f.seek(SeekFrom::Start(pos as u64 + 8))?;
    let mut bytes = Vec::new();
    f.read_to_end(&mut bytes)?;
    Ok(bytes.as_chunks::<2>().0.iter().map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0).collect())
}

pub fn write_wav(path: &Path, samples: &[f32]) -> Result<()> {
    let spec =
        hound::WavSpec { channels: 1, sample_rate: RATE, bits_per_sample: 16, sample_format: hound::SampleFormat::Int };
    let mut w = hound::WavWriter::create(path, spec)?;
    for s in samples {
        w.write_sample((s.clamp(-1.0, 1.0) * 32767.0) as i16)?;
    }
    w.finalize()?;
    Ok(())
}

pub fn ms_to_sample(ms: i64) -> usize {
    (ms.max(0) as usize) * RATE as usize / 1000
}

pub fn sample_to_ms(sample: usize) -> i64 {
    (sample as i64) * 1000 / RATE as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_back_and_survives_truncated_header() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("a.wav");
        let tone: Vec<f32> = (0..1600).map(|i| (i as f32 * 0.05).sin() * 0.5).collect();
        write_wav(&p, &tone).unwrap();
        let back = read_wav(&p).unwrap();
        assert_eq!(back.len(), tone.len());
        assert!((back[100] - tone[100]).abs() < 1e-3);

        // Zero the RIFF and data sizes, as a crash before EOS leaves them.
        let mut bytes = std::fs::read(&p).unwrap();
        bytes[4..8].copy_from_slice(&0u32.to_le_bytes());
        let data = bytes.windows(4).position(|w| w == b"data").unwrap();
        bytes[data + 4..data + 8].copy_from_slice(&0u32.to_le_bytes());
        std::fs::write(&p, bytes).unwrap();
        assert_eq!(read_wav(&p).unwrap().len(), tone.len());
    }
}
