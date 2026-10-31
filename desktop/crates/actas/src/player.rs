//! Playback of a meeting: both WAV tracks mixed, with seeking.

use std::path::Path;

use anyhow::{Context, Result, anyhow};
use gstreamer as gst;
use gstreamer::prelude::*;

pub struct Player {
    pipeline: gst::Pipeline,
    playing: bool,
}

fn branch(path: &Path) -> String {
    let p = path.to_string_lossy().replace('\\', "\\\\").replace('"', "\\\"");
    format!("filesrc location=\"{p}\" ! wavparse ! audioconvert ! audioresample ! mix. ")
}

impl Player {
    pub fn open(tracks: &[&Path]) -> Result<Self> {
        gst::init()?;
        let tracks: Vec<&&Path> = tracks.iter().filter(|p| p.exists()).collect();
        if tracks.is_empty() {
            return Err(anyhow!("la reunión no tiene audio"));
        }
        let mut desc = String::from("audiomixer name=mix ! audioconvert ! autoaudiosink ");
        for t in tracks {
            desc += &branch(t);
        }
        let pipeline =
            gst::parse::launch(&desc)?.downcast::<gst::Pipeline>().map_err(|_| anyhow!("no es un pipeline"))?;
        pipeline.set_state(gst::State::Paused).context("preparando la reproducción")?;
        Ok(Self { pipeline, playing: false })
    }

    pub fn is_playing(&self) -> bool {
        self.playing
    }

    pub fn play(&mut self) {
        let _ = self.pipeline.set_state(gst::State::Playing);
        self.playing = true;
    }

    pub fn pause(&mut self) {
        let _ = self.pipeline.set_state(gst::State::Paused);
        self.playing = false;
    }

    pub fn toggle(&mut self) {
        if self.playing { self.pause() } else { self.play() }
    }

    pub fn seek_ms(&mut self, ms: i64) {
        let _ = self.pipeline.seek_simple(
            gst::SeekFlags::FLUSH | gst::SeekFlags::ACCURATE,
            gst::ClockTime::from_mseconds(ms.max(0) as u64),
        );
    }

    pub fn position_ms(&self) -> i64 {
        self.pipeline.query_position::<gst::ClockTime>().map(|t| t.mseconds() as i64).unwrap_or(0)
    }

    /// Call periodically: handles end of stream. Returns false when finished.
    pub fn poll(&mut self) -> bool {
        let Some(bus) = self.pipeline.bus() else { return true };
        while let Some(msg) = bus.pop() {
            match msg.view() {
                gst::MessageView::Eos(_) => {
                    self.pause();
                    self.seek_ms(0);
                    return false;
                }
                gst::MessageView::Error(e) => {
                    tracing::warn!("reproducción: {}", e.error());
                    self.pause();
                    return false;
                }
                _ => {}
            }
        }
        true
    }
}

impl Drop for Player {
    fn drop(&mut self) {
        let _ = self.pipeline.set_state(gst::State::Null);
    }
}
