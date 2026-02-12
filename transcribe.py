"""Transcription module using Whisper with audio extraction and chunking."""

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import torch
import whisper

from checkpoint import CheckpointManager, ChunkSpec, ShutdownCoordinator
from config import Config as AppConfig

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    """Result of a transcription job."""

    success: bool
    text: str
    key: str
    local_path: Optional[Path] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None


@dataclass
class TranscriptionJob:
    """Represents a transcription job for a video file."""

    key: str
    size_bytes: int
    last_modified: datetime
    local_path: Optional[Path] = None
    status: str = "pending"
    result: Optional["TranscriptionResult"] = None


class TranscriptionService:
    """Service for transcribing video/audio files using Whisper."""

    SUPPORTED_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg")
    SUPPORTED_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm")

    def __init__(self, config: AppConfig):
        """Initialize transcription service.

        Args:
            config: Application configuration.
        """
        self.config = config
        self._model = None

    @property
    def model(self):
        """Lazy load Whisper model."""
        if self._model is None:
            logger.info(
                f"Loading Whisper model '{self.config.model_size}' on {self.config.device}"
            )

            # Check CUDA availability
            device = self.config.device
            if device == "cuda" and not torch.cuda.is_available():
                logger.warning("CUDA requested but not available, falling back to CPU")
                device = "cpu"

            self._model = whisper.load_model(self.config.model_size, device=device)
            logger.info(f"Whisper model loaded on {device}")

        return self._model

    def validate_file(self, key: str, size_bytes: int) -> Tuple[bool, str]:
        """Validate file type and size.

        Args:
            key: S3 object key.
            size_bytes: File size in bytes.

        Returns:
            Tuple of (is_valid, error_message).
        """
        ext = Path(key).suffix.lower()

        # Check extension
        allowed_exts = self.SUPPORTED_AUDIO_EXTS + self.SUPPORTED_VIDEO_EXTS
        if ext not in allowed_exts:
            return False, f"Unsupported file extension: {ext}. Allowed: {allowed_exts}"

        # Check size
        if size_bytes > self.config.max_file_size_bytes:
            max_mb = self.config.max_file_size_bytes / (1024 * 1024)
            return False, (
                f"File size {size_bytes / (1024 * 1024):.1f}MB exceeds maximum "
                f"of {max_mb:.0f}MB"
            )

        return True, ""

    def extract_audio_from_video(self, video_path: Path, output_path: Path) -> Path:
        """Extract audio from video file using ffmpeg.

        Args:
            video_path: Path to video file.
            output_path: Path to output audio file.

        Returns:
            Path to extracted audio file.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Extracting audio from {video_path} to {output_path}")

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            str(output_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info(f"Audio extracted successfully: {output_path}")
            return output_path
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to extract audio from {video_path}: {e.stderr}")

    def get_audio_duration(self, audio_path: Path) -> float:
        """Get duration of audio file using ffprobe.

        Args:
            audio_path: Path to audio file.

        Returns:
            Duration in seconds.
        """
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())

    def split_audio_into_chunks(
        self,
        audio_path: Path,
        chunk_seconds: int,
        tmp_dir: Path,
        total_duration: Optional[float] = None,
    ) -> List[Path]:
        """Split audio file into chunks for large file processing.

        Args:
            audio_path: Path to audio file.
            chunk_seconds: Chunk length in seconds.
            tmp_dir: Directory for temporary chunk files.
            total_duration: Optional pre-computed duration to avoid re-probing.

        Returns:
            List of paths to chunk files.
        """
        if total_duration is None:
            total_duration = self.get_audio_duration(audio_path)
        num_chunks = max(1, math.ceil(total_duration / chunk_seconds))

        logger.info(
            f"Splitting audio {audio_path} ({total_duration:.1f}s) into "
            f"{num_chunks} chunks of {chunk_seconds}s each"
        )

        chunk_paths = []
        for i in range(num_chunks):
            start = i * chunk_seconds
            out_path = tmp_dir / f"chunk_{i:04d}.wav"

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(audio_path),
                "-ss",
                str(start),
                "-t",
                str(chunk_seconds),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-acodec",
                "pcm_s16le",
                str(out_path),
            ]

            try:
                subprocess.run(cmd, capture_output=True, check=True)
                if out_path.exists() and out_path.stat().st_size > 100:
                    chunk_paths.append(out_path)
                elif out_path.exists():
                    out_path.unlink()
            except subprocess.CalledProcessError:
                logger.warning(f"Failed to extract chunk {i}, continuing with existing")
                break

        logger.info(f"Created {len(chunk_paths)} audio chunks")
        return chunk_paths

    def transcribe_audio(
        self,
        audio_path: Path,
        output_path: Path,
        language: str = "es",
        checkpoint_db: Optional[str] = None,
        resume_checkpoint: Optional[bool] = None,
        reset_checkpoint: bool = False,
        checkpoint_sync_s3_uri: Optional[str] = None,
        shutdown_coordinator: Optional[ShutdownCoordinator] = None,
    ) -> TranscriptionResult:
        """Transcribe an audio file using Whisper.

        Args:
            audio_path: Path to audio file.
            output_path: Path to output transcription file.
            language: Language code for transcription.
            checkpoint_db: Optional path to checkpoint database.
            resume_checkpoint: Whether to resume from existing checkpoint.
            reset_checkpoint: Whether to reset checkpoint before starting.
            checkpoint_sync_s3_uri: Optional S3 URI for checkpoint sync.
            shutdown_coordinator: Optional shutdown coordinator for graceful stop.

        Returns:
            TranscriptionResult with success status and text.
        """
        start_time = time.time()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            logger.info(f"Starting transcription of {audio_path}")

            # Check if chunking is needed
            chunk_seconds = self.config.transcript_chunk_seconds
            if chunk_seconds > 0:
                if shutdown_coordinator and shutdown_coordinator.should_stop():
                    raise RuntimeError("Shutdown requested before chunk processing")
                result_text = self._transcribe_with_chunking(
                    audio_path,
                    chunk_seconds,
                    language,
                    output_path,
                    checkpoint_db=checkpoint_db,
                    resume_checkpoint=resume_checkpoint,
                    reset_checkpoint=reset_checkpoint,
                    checkpoint_sync_s3_uri=checkpoint_sync_s3_uri,
                    shutdown_coordinator=shutdown_coordinator,
                )
            else:
                if shutdown_coordinator and shutdown_coordinator.should_stop():
                    raise RuntimeError("Shutdown requested before transcription")
                # Single pass transcription
                result: dict[str, Any] = self.model.transcribe(
                    str(audio_path), language=language
                )
                result_text = str(result.get("text", "")).strip()
                # Save transcription to file
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(result_text)

            duration = time.time() - start_time
            logger.info(
                f"Transcription completed in {duration:.1f}s: "
                f"{audio_path} -> {output_path}"
            )

            return TranscriptionResult(
                success=True,
                text=result_text,
                key=str(output_path),
                local_path=output_path,
                duration_seconds=duration,
            )

        except Exception as e:
            logger.error(f"Transcription failed for {audio_path}: {e}")
            return TranscriptionResult(
                success=False,
                text="",
                key=str(output_path),
                error=str(e),
            )

    def _transcribe_with_chunking(
        self,
        audio_path: Path,
        chunk_seconds: int,
        language: str,
        output_path: Path,
        checkpoint_db: Optional[str] = None,
        resume_checkpoint: Optional[bool] = None,
        reset_checkpoint: bool = False,
        checkpoint_sync_s3_uri: Optional[str] = None,
        shutdown_coordinator: Optional[ShutdownCoordinator] = None,
    ) -> str:
        """Transcribe audio using chunking with per-chunk checkpointing.

        Args:
            audio_path: Path to audio file.
            chunk_seconds: Chunk length in seconds.
            language: Language code.
            output_path: Path to output transcription file.
            checkpoint_db: Optional path to checkpoint database.
            resume_checkpoint: Whether to resume from existing checkpoint.
            reset_checkpoint: Whether to reset checkpoint before starting.
            checkpoint_sync_s3_uri: Optional S3 URI for checkpoint sync.
            shutdown_coordinator: Optional shutdown coordinator for graceful stop.

        Returns:
            Combined transcription text.
        """
        total_duration = self.get_audio_duration(audio_path)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            chunk_paths = self.split_audio_into_chunks(
                audio_path, chunk_seconds, tmp_path, total_duration=total_duration
            )

            if not chunk_paths:
                raise RuntimeError("No audio chunks available for transcription")

            chunk_dir = output_path.parent / f"{output_path.stem}_chunks"
            plan_meta = {
                "chunk_seconds": chunk_seconds,
                "language": language,
                "model_size": self.config.model_size,
            }
            chunk_specs: List[ChunkSpec] = []
            for idx in range(len(chunk_paths)):
                start = idx * chunk_seconds
                end = min(total_duration, (idx + 1) * chunk_seconds)
                payload = {
                    **plan_meta,
                    "index": idx,
                    "start_seconds": start,
                    "end_seconds": end,
                }
                chunk_specs.append(
                    ChunkSpec(
                        index=idx,
                        start_seconds=start,
                        end_seconds=end,
                        plan_hash=self._chunk_plan_hash(payload),
                    )
                )

            db_path = self._resolve_checkpoint_db_path(output_path, checkpoint_db)
            checkpoint = CheckpointManager(
                db_path=db_path,
                source_uri=str(audio_path),
                fingerprint=self._compute_source_fingerprint(audio_path),
                total_chunks=len(chunk_specs),
                config=self.config,
                resume=(
                    resume_checkpoint
                    if resume_checkpoint is not None
                    else self.config.resume_checkpoint
                ),
                reset=reset_checkpoint or self.config.reset_checkpoint,
                sync_s3_uri=checkpoint_sync_s3_uri
                or self.config.checkpoint_sync_s3_uri,
            )
            checkpoint.register_chunks(chunk_specs)
            chunk_map = {spec.index: chunk_paths[spec.index] for spec in chunk_specs}

            try:
                while True:
                    if shutdown_coordinator and shutdown_coordinator.should_stop():
                        raise RuntimeError(
                            "Shutdown requested before processing next chunk"
                        )
                    spec = checkpoint.claim_next_chunk()
                    if spec is None:
                        break
                    chunk_index = spec[0]
                    chunk_path = chunk_map.get(chunk_index)
                    if not chunk_path or not chunk_path.exists():
                        error_msg = f"Chunk audio missing for index {chunk_index}"
                        logger.error(error_msg)
                        checkpoint.mark_chunk_failed(
                            chunk_index, error_msg, permanent=True
                        )
                        continue

                    logger.info(
                        "Transcribing chunk %d/%d", chunk_index + 1, len(chunk_specs)
                    )
                    result: dict[str, Any] = self.model.transcribe(
                        str(chunk_path), language=language
                    )
                    text = str(result.get("text", "")).strip()
                    chunk_artifact_path, sha = checkpoint.write_chunk_artifact(
                        chunk_dir, chunk_index, text
                    )
                    checkpoint.mark_chunk_done(
                        chunk_index, str(chunk_artifact_path), sha
                    )

                if shutdown_coordinator and shutdown_coordinator.should_stop():
                    raise RuntimeError("Shutdown requested during chunk processing")

                return self._finalize_chunk_transcripts(
                    chunk_dir, len(chunk_specs), output_path, checkpoint
                )
            finally:
                checkpoint.close()

    def _finalize_chunk_transcripts(
        self,
        chunk_dir: Path,
        total_chunks: int,
        output_path: Path,
        checkpoint: CheckpointManager,
    ) -> str:
        """Assemble chunk artifacts into the final transcript file."""
        parts: List[str] = []
        for idx in range(total_chunks):
            chunk_file = chunk_dir / f"chunk_{idx:04d}.txt"
            if not chunk_file.exists():
                logger.warning("Missing chunk artifact %s", chunk_file)
                continue
            text = chunk_file.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)

        combined = "\n".join(parts)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as final_file:
            final_file.write(combined)
            final_file.flush()
            os.fsync(final_file.fileno())
        final_sha = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        checkpoint.persist_final_output(str(output_path), final_sha)
        logger.info("Chunked transcription saved to %s", output_path)
        return combined

    def _resolve_checkpoint_db_path(
        self, output_path: Path, override: Optional[str]
    ) -> Path:
        """Determine checkpoint DB path, falling back to local namespace."""
        if override and override.startswith("s3://"):
            logger.warning(
                "S3 checkpoint DB URIs are not fully supported; using local default"
            )
        if override and not override.startswith("s3://"):
            path = Path(override)
            if not path.is_absolute():
                path = output_path.parent / override
            return path
        return output_path.parent / ".mt_checkpoints" / output_path.stem / "ckpt.sqlite"

    def _compute_source_fingerprint(self, audio_path: Path) -> str:
        """Compute a fingerprint for the source audio file."""
        try:
            stat = audio_path.stat()
            return f"{audio_path}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            return f"{audio_path}:unknown"

    def _chunk_plan_hash(self, payload: dict) -> str:
        """Compute a deterministic hash for chunk plan metadata."""
        normalized = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def process_video(
        self,
        video_path: Path,
        output_path: Path,
        checkpoint_db: Optional[str] = None,
        resume_checkpoint: Optional[bool] = None,
        reset_checkpoint: bool = False,
        checkpoint_sync_s3_uri: Optional[str] = None,
        shutdown_coordinator: Optional[ShutdownCoordinator] = None,
    ) -> TranscriptionResult:
        """Process a video file: extract audio and transcribe.

        Args:
            video_path: Path to video file.
            output_path: Path to output transcription file.
            checkpoint_db: Optional path to checkpoint database.
            resume_checkpoint: Whether to resume from existing checkpoint.
            reset_checkpoint: Whether to reset checkpoint before starting.
            checkpoint_sync_s3_uri: Optional S3 URI for checkpoint sync.
            shutdown_coordinator: Optional shutdown coordinator for graceful stop.

        Returns:
            TranscriptionResult.
        """
        start_time = time.time()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            ext = video_path.suffix.lower()

            # Extract audio if video
            if ext in self.SUPPORTED_VIDEO_EXTS:
                audio_path = output_path.with_suffix(".wav")
                self.extract_audio_from_video(video_path, audio_path)
            else:
                audio_path = video_path

            # Transcribe
            result = self.transcribe_audio(
                audio_path,
                output_path.with_suffix(".txt"),
                language="es",
                checkpoint_db=checkpoint_db,
                resume_checkpoint=resume_checkpoint,
                reset_checkpoint=reset_checkpoint,
                checkpoint_sync_s3_uri=checkpoint_sync_s3_uri,
                shutdown_coordinator=shutdown_coordinator,
            )

            # Cleanup temp audio if created
            if audio_path != video_path and audio_path.exists():
                audio_path.unlink()

            return result

        except Exception as e:
            logger.error(f"Failed to process video {video_path}: {e}")
            return TranscriptionResult(
                success=False,
                text="",
                key=str(output_path),
                error=str(e),
            )

    def cleanup(self):
        """Clean up resources (model memory)."""
        if self._model is not None:
            del self._model
            self._model = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.info("Whisper model unloaded")
