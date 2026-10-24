"""Lazy, persistent WhisperX transcription pipeline."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Mapping

from study.adapters.transcription.artifacts import (
    ArtifactReference,
    build_transcript,
    publish_transcript,
    reference_for_file,
    sanitize_error,
    sha256_file,
    write_json_artifact,
)

PIPELINE_REVISION = "whisperx-worker-v1"
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
_SHA256_LENGTH = 64
_ALLOWED_REQUEST_FIELDS = {
    "source_path",
    "source_sha256",
    "staging_dir",
    "media_kind",
    "language",
    "assignment",
    "min_speakers",
    "max_speakers",
    "start_phase",
    "checkpoints",
}

ProgressCallback = Callable[[dict[str, str]], None]


@dataclass(frozen=True)
class PipelineConfig:
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "auto"
    default_language: str = "es"
    batch_size: int = 16
    model_cache: str | None = None
    local_files_only: bool = True
    diarization_model: str = DIARIZATION_MODEL

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        environment = os.environ if environment is None else environment
        return cls(
            model=environment.get("ACADEMIC_MODEL", "large-v3"),
            device=environment.get("ACADEMIC_DEVICE", "auto"),
            compute_type=environment.get("ACADEMIC_COMPUTE_TYPE", "auto"),
            default_language=environment.get("ACADEMIC_LANGUAGE", "es"),
            batch_size=int(environment.get("ACADEMIC_BATCH_SIZE", "16")),
            model_cache=environment.get("ACADEMIC_MODEL_CACHE") or None,
            local_files_only=_environment_flag(
                environment.get("ACADEMIC_LOCAL_FILES_ONLY", "1")
            ),
        )

    def validate(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("device must be auto, cpu, or cuda")
        if self.compute_type not in {
            "auto",
            "default",
            "float16",
            "float32",
            "int8",
            "int8_float16",
            "int8_float32",
        }:
            raise ValueError("compute_type is invalid")
        if self.default_language not in {"auto", "en", "es"}:
            raise ValueError("default_language must be auto, en, or es")
        if self.batch_size < 1:
            raise ValueError("batch_size must be positive")


@dataclass(frozen=True)
class TranscriptionRequest:
    source_path: Path
    source_sha256: str
    staging_dir: Path
    media_kind: str
    language: str
    assignment: Mapping[str, Any]
    min_speakers: int | None
    max_speakers: int | None
    start_phase: str
    checkpoints: Mapping[str, ArtifactReference]


class PipelineError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        artifacts: Mapping[str, ArtifactReference] | None = None,
    ):
        super().__init__(sanitize_error(message))
        self.code = code
        self.artifacts = dict(artifacts or {})


class WhisperXPipeline:
    """Owns model caches for the lifetime of one worker process."""

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config or PipelineConfig.from_environment()
        self.config.validate()
        self._asr_models: dict[tuple[str, str, str], Any] = {}
        self._alignment_models: dict[tuple[str, str], tuple[Any, Any]] = {}
        self._diarization_models: dict[tuple[str, str], Any] = {}

    @property
    def signature(self) -> str:
        payload = {
            "revision": PIPELINE_REVISION,
            "model": self.config.model,
            "device": self.config.device,
            "compute_type": self.config.compute_type,
            "diarization_model": self.config.diarization_model,
            "local_files_only": self.config.local_files_only,
            "batch_size": self.config.batch_size,
            "packages": {
                name: _distribution_version(name)
                for name in ("whisperx", "torch", "torchaudio", "torchvision")
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def transcribe(
        self,
        parameters: Mapping[str, Any],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        request = self._parse_request(parameters)
        request.staging_dir.mkdir(parents=True, exist_ok=True)
        artifacts: dict[str, ArtifactReference] = {}

        self._verify_source(request.source_path, request.source_sha256)
        duration_ms = self._probe_duration_ms(request.source_path)
        device, compute_type = self._resolve_profile()
        whisperx = self._import_whisperx()
        audio_path = request.source_path
        if request.media_kind == "video":
            audio_reference = self._extract_audio(
                request.source_path, request.staging_dir
            )
            artifacts["audio"] = audio_reference
            audio_path = Path(audio_reference.path)
        audio = whisperx.load_audio(str(audio_path))

        phases = {"asr": "skipped", "alignment": "skipped", "diarization": "skipped"}
        errors: list[dict[str, str]] = []
        requested_language = request.language
        active_result: Mapping[str, Any]
        detected_language: str
        if request.start_phase == "asr":
            _notify(progress, "asr", "started")
            try:
                asr_model = self._get_asr_model(whisperx, device, compute_type)
                asr_result = asr_model.transcribe(
                    audio,
                    batch_size=self.config.batch_size,
                    language=None
                    if requested_language == "auto"
                    else requested_language,
                    print_progress=False,
                    verbose=False,
                )
                detected_language = str(
                    asr_result.get("language")
                    or (requested_language if requested_language != "auto" else "")
                )
                if not detected_language:
                    raise ValueError("ASR did not report a detected language")
                active_result = asr_result
                phases["asr"] = "completed"
                artifacts["asr"] = self._write_phase(
                    request,
                    duration_ms,
                    "asr",
                    "completed",
                    detected_language,
                    active_result,
                )
                _notify(progress, "asr", "completed")
            except Exception as error:
                message = sanitize_error(error)
                phases["asr"] = "failed"
                artifacts["asr"] = self._write_phase(
                    request, duration_ms, "asr", "failed", None, {}, message
                )
                _notify(progress, "asr", "failed")
                raise PipelineError(
                    "asr_failed", message, artifacts=artifacts
                ) from error
        else:
            active_result, detected_language = self._load_checkpoint(
                request, "asr" if request.start_phase == "alignment" else "alignment"
            )
            phases["asr"] = "completed"
            artifacts["asr"] = request.checkpoints["asr"]
            if request.start_phase == "diarization":
                phases["alignment"] = "completed"
                artifacts["alignment"] = request.checkpoints["alignment"]

        if request.start_phase != "diarization":
            _notify(progress, "alignment", "started")
            try:
                align_model, metadata = self._get_alignment_model(
                    whisperx, detected_language, device
                )
                active_result = whisperx.align(
                    active_result["segments"],
                    align_model,
                    metadata,
                    audio,
                    device,
                    return_char_alignments=False,
                )
                phases["alignment"] = "completed"
                artifacts["alignment"] = self._write_phase(
                    request,
                    duration_ms,
                    "alignment",
                    "completed",
                    detected_language,
                    active_result,
                )
                _notify(progress, "alignment", "completed")
            except Exception as error:
                message = sanitize_error(error)
                phases["alignment"] = "failed"
                errors.append({"phase": "alignment", "message": message})
                artifacts["alignment"] = self._write_phase(
                    request,
                    duration_ms,
                    "alignment",
                    "failed",
                    detected_language,
                    active_result,
                    message,
                )
                _notify(progress, "alignment", "failed")

        _notify(progress, "diarization", "started")
        try:
            diarization = self._get_diarization_model(whisperx, device)
            diarization_options = {
                name: value
                for name, value in {
                    "min_speakers": request.min_speakers,
                    "max_speakers": request.max_speakers,
                }.items()
                if value is not None
            }
            diarized_segments = diarization(audio, **diarization_options)
            active_result = whisperx.assign_word_speakers(
                diarized_segments, active_result, fill_nearest=True
            )
            phases["diarization"] = "completed"
            artifacts["diarization"] = self._write_phase(
                request,
                duration_ms,
                "diarization",
                "completed",
                detected_language,
                active_result,
            )
            _notify(progress, "diarization", "completed")
        except Exception as error:
            message = sanitize_error(error)
            phases["diarization"] = "failed"
            errors.append({"phase": "diarization", "message": message})
            artifacts["diarization"] = self._write_phase(
                request,
                duration_ms,
                "diarization",
                "failed",
                detected_language,
                active_result,
                message,
            )
            _notify(progress, "diarization", "failed")

        self._verify_source(
            request.source_path, request.source_sha256, artifacts=artifacts
        )
        transcript = build_transcript(
            source_sha256=request.source_sha256,
            duration_ms=duration_ms,
            assignment=request.assignment,
            requested_language=requested_language,
            detected_language=detected_language,
            revisions={
                "pipeline": PIPELINE_REVISION,
                "asr": self.config.model,
                "alignment": f"default:{detected_language}:whisperx-{_distribution_version('whisperx')}",
                "diarization": f"{self.config.diarization_model}:whisperx-{_distribution_version('whisperx')}",
            },
            phases=phases,
            errors=errors,
            segments=active_result.get("segments", []),
        )
        published = publish_transcript(request.staging_dir, transcript)
        artifacts["transcript_json"] = published["json"]
        artifacts["transcript_markdown"] = published["markdown"]
        completed_phases = [
            phase for phase, status in phases.items() if status == "completed"
        ]
        failed_phases = [
            phase for phase, status in phases.items() if status == "failed"
        ]
        return {
            "status": transcript["status"],
            "assigned": all(transcript["assignment"].values()),
            "completed_phases": completed_phases,
            "failed_phases": failed_phases,
            "error": "; ".join(error["message"] for error in errors) or None,
            "pipeline_signature": self.signature,
            "profile": {"device": device, "compute_type": compute_type},
            "artifacts": {
                name: reference.to_wire()
                for name, reference in sorted(artifacts.items())
            },
        }

    def _load_checkpoint(
        self, request: TranscriptionRequest, phase: str
    ) -> tuple[Mapping[str, Any], str]:
        required = ("asr",) if phase == "asr" else ("asr", "alignment")
        if any(name not in request.checkpoints for name in required):
            raise PipelineError(
                "checkpoint_invalid", f"resume at {phase} requires prior checkpoints"
            )
        reference = request.checkpoints[phase]
        try:
            payload = json.loads(Path(reference.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PipelineError(
                "checkpoint_invalid", "checkpoint artifact is not readable JSON"
            ) from error
        if (
            not isinstance(payload, Mapping)
            or payload.get("version") != 1
            or payload.get("phase") != phase
            or payload.get("status") != "completed"
            or payload.get("source_sha256") != request.source_sha256
            or payload.get("pipeline_revision") != PIPELINE_REVISION
            or not isinstance(payload.get("raw_segments"), list)
            or not isinstance(payload.get("language"), str)
        ):
            raise PipelineError(
                "checkpoint_invalid", "checkpoint metadata does not match this job"
            )
        return {"segments": payload["raw_segments"]}, str(payload["language"])

    def _parse_request(self, parameters: Mapping[str, Any]) -> TranscriptionRequest:
        if not isinstance(parameters, Mapping):
            raise PipelineError("invalid_request", "params must be an object")
        unexpected = sorted(set(parameters) - _ALLOWED_REQUEST_FIELDS)
        if unexpected:
            raise PipelineError(
                "invalid_request", f"Unsupported transcribe field: {unexpected[0]}"
            )
        try:
            source_path = Path(str(parameters["source_path"])).resolve(strict=True)
            staging_dir = Path(str(parameters["staging_dir"])).resolve()
            source_sha256 = str(parameters["source_sha256"])
            media_kind = str(parameters["media_kind"])
        except (KeyError, OSError) as error:
            raise PipelineError("invalid_request", str(error)) from error
        if not source_path.is_file():
            raise PipelineError("invalid_request", "source_path must be a file")
        if len(source_sha256) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise PipelineError("invalid_request", "source_sha256 is invalid")
        if media_kind not in {"audio", "video"}:
            raise PipelineError("invalid_request", "media_kind must be audio or video")
        language = str(parameters.get("language", self.config.default_language))
        if language not in {"auto", "en", "es"}:
            raise PipelineError("invalid_request", "language must be auto, en, or es")
        assignment = parameters.get("assignment") or {}
        if not isinstance(assignment, Mapping):
            raise PipelineError("invalid_request", "assignment must be an object")
        unexpected_assignment = sorted(set(assignment) - {"course", "class_date"})
        if unexpected_assignment:
            raise PipelineError(
                "invalid_request",
                f"Unsupported assignment field: {unexpected_assignment[0]}",
            )
        min_speakers = _optional_positive_int(parameters.get("min_speakers"))
        max_speakers = _optional_positive_int(parameters.get("max_speakers"))
        if min_speakers and max_speakers and min_speakers > max_speakers:
            raise PipelineError(
                "invalid_request", "min_speakers cannot exceed max_speakers"
            )
        start_phase = str(parameters.get("start_phase", "asr"))
        if start_phase not in {"asr", "alignment", "diarization"}:
            raise PipelineError("invalid_request", "start_phase is invalid")
        checkpoints = self._parse_checkpoints(
            parameters.get("checkpoints", []), staging_dir
        )
        return TranscriptionRequest(
            source_path=source_path,
            source_sha256=source_sha256,
            staging_dir=staging_dir,
            media_kind=media_kind,
            language=language,
            assignment=dict(assignment),
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            start_phase=start_phase,
            checkpoints=checkpoints,
        )

    def _parse_checkpoints(
        self, value: Any, staging_dir: Path
    ) -> Mapping[str, ArtifactReference]:
        if not isinstance(value, list):
            raise PipelineError("invalid_request", "checkpoints must be an array")
        checkpoints: dict[str, ArtifactReference] = {}
        for raw in value:
            if not isinstance(raw, Mapping) or set(raw) != {"phase", "path", "sha256"}:
                raise PipelineError("invalid_request", "checkpoint shape is invalid")
            phase = str(raw["phase"])
            if phase not in {"asr", "alignment", "diarization"} or phase in checkpoints:
                raise PipelineError("invalid_request", "checkpoint phase is invalid")
            expected_sha = str(raw["sha256"])
            try:
                path = Path(str(raw["path"])).resolve(strict=True)
                path.relative_to(staging_dir)
            except (OSError, ValueError) as error:
                raise PipelineError(
                    "checkpoint_invalid", "checkpoint is outside job staging"
                ) from error
            if not path.is_file() or sha256_file(path) != expected_sha:
                raise PipelineError(
                    "checkpoint_invalid", "checkpoint artifact hash does not match"
                )
            checkpoints[phase] = ArtifactReference(str(path), expected_sha)
        return checkpoints

    def _resolve_profile(self) -> tuple[str, str]:
        device = self.config.device
        if device == "auto":
            torch = importlib.import_module("torch")
            device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = self.config.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def _import_whisperx(self):
        if self.config.local_files_only:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        return importlib.import_module("whisperx")

    def _verify_source(
        self,
        source: Path,
        expected_sha256: str,
        *,
        artifacts: Mapping[str, ArtifactReference] | None = None,
    ) -> None:
        try:
            actual_hash = sha256_file(source)
        except OSError as error:
            raise PipelineError(
                "source_changed", str(error), artifacts=artifacts
            ) from error
        if actual_hash != expected_sha256:
            raise PipelineError(
                "source_changed",
                "Source SHA-256 does not match request",
                artifacts=artifacts,
            )

    def _get_asr_model(self, whisperx, device: str, compute_type: str):
        key = (self.config.model, device, compute_type)
        if key not in self._asr_models:
            options: dict[str, Any] = {
                "compute_type": compute_type,
                "language": None,
                "local_files_only": self.config.local_files_only,
            }
            if self.config.model_cache:
                options["download_root"] = self.config.model_cache
            self._asr_models[key] = whisperx.load_model(
                self.config.model, device, **options
            )
        return self._asr_models[key]

    def _get_alignment_model(self, whisperx, language: str, device: str):
        key = (language, device)
        if key not in self._alignment_models:
            options: dict[str, Any] = {
                "language_code": language,
                "device": device,
                "model_cache_only": self.config.local_files_only,
            }
            if self.config.model_cache:
                options["model_dir"] = self.config.model_cache
            self._alignment_models[key] = whisperx.load_align_model(**options)
        return self._alignment_models[key]

    def _get_diarization_model(self, whisperx, device: str):
        key = (self.config.diarization_model, device)
        if key not in self._diarization_models:
            token = os.environ.get("HF_TOKEN") or None
            options: dict[str, Any] = {
                "model_name": self.config.diarization_model,
                "device": device,
                "token": token,
            }
            if self.config.model_cache:
                options["cache_dir"] = self.config.model_cache
            try:
                pipeline_type = getattr(whisperx, "DiarizationPipeline", None)
                if pipeline_type is None:
                    pipeline_type = importlib.import_module(
                        "whisperx.diarize"
                    ).DiarizationPipeline
                self._diarization_models[key] = pipeline_type(**options)
            finally:
                if token:
                    os.environ.pop("HF_TOKEN", None)
        return self._diarization_models[key]

    def _probe_duration_ms(self, source: Path) -> int:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            duration = float(completed.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise PipelineError("probe_failed", str(error)) from error
        if not math.isfinite(duration) or duration < 0:
            raise PipelineError("probe_failed", "ffprobe returned an invalid duration")
        return int(round(duration * 1000))

    def _extract_audio(self, source: Path, staging_dir: Path) -> ArtifactReference:
        destination = staging_dir / "audio.wav"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".audio.", suffix=".wav", dir=staging_dir
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary_path),
                ],
                check=True,
                capture_output=True,
            )
            with open(temporary_path, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
            _fsync_directory(staging_dir)
            return reference_for_file(destination)
        except (OSError, subprocess.SubprocessError) as error:
            raise PipelineError("audio_extraction_failed", str(error)) from error
        finally:
            temporary_path.unlink(missing_ok=True)

    def _write_phase(
        self,
        request: TranscriptionRequest,
        duration_ms: int,
        phase: str,
        status: str,
        detected_language: str | None,
        result: Mapping[str, Any],
        error: str | None = None,
    ) -> ArtifactReference:
        snapshot = build_transcript(
            source_sha256=request.source_sha256,
            duration_ms=duration_ms,
            assignment=request.assignment,
            requested_language=request.language,
            detected_language=detected_language,
            revisions={
                "pipeline": PIPELINE_REVISION,
                "asr": self.config.model,
                "alignment": f"default:{detected_language}"
                if detected_language
                else None,
                "diarization": self.config.diarization_model,
            },
            phases={
                "asr": status if phase == "asr" else "skipped",
                "alignment": status if phase == "alignment" else "skipped",
                "diarization": status if phase == "diarization" else "skipped",
            },
            errors=[{"phase": phase, "message": error}] if error else [],
            segments=result.get("segments", []),
        )
        payload = {
            "version": 1,
            "phase": phase,
            "status": status,
            "source_sha256": request.source_sha256,
            "duration_ms": duration_ms,
            "language": detected_language,
            "pipeline_revision": PIPELINE_REVISION,
            "error": sanitize_error(error) if error else None,
            "speakers": snapshot["speakers"],
            "segments": snapshot["segments"],
            "raw_segments": _json_safe(result.get("segments", [])),
        }
        return write_json_artifact(request.staging_dir / f"{phase}.json", payload)


def _notify(progress: ProgressCallback | None, phase: str, state: str) -> None:
    if progress:
        progress({"phase": phase, "state": state})


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PipelineError(
            "invalid_request", "speaker counts must be positive integers"
        )
    return value


def _environment_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("ACADEMIC_LOCAL_FILES_ONLY must be a boolean")


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "uninstalled"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        return _json_safe(item())
    raise TypeError(
        f"checkpoint value is not JSON serializable: {type(value).__name__}"
    )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
