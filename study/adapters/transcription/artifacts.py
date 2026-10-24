"""Canonical transcript rendering and atomic artifact publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
_PHASE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^/\s:'\"]+/)*[^/\s:'\"]+")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s:'\"]+\\?)+")
_SECRET_RE = re.compile(
    r"(?i)\b(token|password|secret|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_USERNAME_RE = re.compile(r"(?i)\b(user(?:name)?|login)\b\s*[:=]?\s*[^\s,;]+")
_HF_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{6,}\b")


@dataclass(frozen=True)
class ArtifactReference:
    """An IPC-safe reference to a published staging artifact."""

    path: str
    sha256: str

    def to_wire(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_for_file(path: str | os.PathLike[str]) -> ArtifactReference:
    resolved = Path(path).resolve()
    return ArtifactReference(str(resolved), sha256_file(resolved))


def atomic_write_bytes(
    path: str | os.PathLike[str], content: bytes
) -> ArtifactReference:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        _fsync_directory(destination.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return reference_for_file(destination)


def write_json_artifact(
    path: str | os.PathLike[str], payload: Mapping[str, Any]
) -> ArtifactReference:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return atomic_write_bytes(path, f"{serialized}\n".encode())


def sanitize_error(error: BaseException | str) -> str:
    """Remove credentials, usernames, and local paths from persisted errors."""

    message = " ".join(str(error).split()) or "Unknown worker error"
    message = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    message = _USERNAME_RE.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    message = _HF_TOKEN_RE.sub("<redacted>", message)
    message = _WINDOWS_PATH_RE.sub("<path>", message)
    message = _ABSOLUTE_PATH_RE.sub("<path>", message)
    for variable in ("USER", "LOGNAME"):
        value = os.environ.get(variable, "")
        if len(value) >= 3:
            message = message.replace(value, "<user>")
    return message[:500]


def build_transcript(
    *,
    source_sha256: str,
    duration_ms: int,
    assignment: Mapping[str, Any] | None,
    requested_language: str,
    detected_language: str | None,
    revisions: Mapping[str, str | None],
    phases: Mapping[str, str],
    errors: Sequence[Mapping[str, str]],
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the path-free canonical transcript JSON v1 document."""

    if not _SHA256_RE.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("duration_ms must be a non-negative integer")
    if requested_language not in {"auto", "en", "es"}:
        raise ValueError("requested_language must be auto, en, or es")
    if detected_language is not None and not _LANGUAGE_RE.fullmatch(detected_language):
        raise ValueError("detected_language is invalid")

    safe_assignment = _normalize_assignment(assignment)
    normalized_segments, anonymous_ids = _normalize_segments(segments)
    safe_errors = []
    for item in errors:
        phase = str(item.get("phase", "worker"))
        if not _PHASE_RE.fullmatch(phase):
            raise ValueError("error phase is invalid")
        safe_errors.append(
            {
                "phase": phase,
                "message": sanitize_error(
                    str(item.get("message", "Unknown worker error"))
                ),
            }
        )
    safe_phases = {str(name): str(status) for name, status in sorted(phases.items())}
    if set(safe_phases) != {"asr", "alignment", "diarization"}:
        raise ValueError("phases must contain asr, alignment, and diarization")
    if any(
        status not in {"completed", "failed", "skipped"}
        for status in safe_phases.values()
    ):
        raise ValueError("phase status is invalid")
    status = (
        "partial" if safe_errors or "failed" in safe_phases.values() else "complete"
    )

    return {
        "version": 1,
        "status": status,
        "source": {"sha256": source_sha256, "duration_ms": duration_ms},
        "assignment": safe_assignment,
        "language": {
            "requested": requested_language,
            "detected": detected_language,
        },
        "revisions": {
            "pipeline": _safe_revision(revisions.get("pipeline")),
            "asr": _safe_revision(revisions.get("asr")),
            "alignment": _safe_revision(revisions.get("alignment")),
            "diarization": _safe_revision(revisions.get("diarization")),
        },
        "phases": safe_phases,
        "errors": safe_errors,
        "speakers": {
            "anonymous_ids": anonymous_ids,
            "display_names": {speaker: speaker for speaker in anonymous_ids},
        },
        "segments": normalized_segments,
    }


def render_markdown(transcript: Mapping[str, Any]) -> str:
    """Render deterministic Markdown from a canonical transcript."""

    source = transcript["source"]
    assignment = transcript["assignment"]
    language = transcript["language"]
    revisions = transcript["revisions"]
    failed_phases = sorted(
        name for name, status in transcript["phases"].items() if status == "failed"
    )
    frontmatter = [
        "---",
        f"transcript-version: {transcript['version']}",
        f"status: {_yaml_scalar(transcript['status'])}",
        f"source-sha256: {_yaml_scalar(source['sha256'])}",
        f"duration-ms: {source['duration_ms']}",
        f"course: {_yaml_scalar(assignment['course'])}",
        f"class-date: {_yaml_scalar(assignment['class_date'])}",
        f"requested-language: {_yaml_scalar(language['requested'])}",
        f"detected-language: {_yaml_scalar(language['detected'])}",
        f"pipeline-revision: {_yaml_scalar(revisions['pipeline'])}",
        f"asr-model: {_yaml_scalar(revisions['asr'])}",
        f"alignment-model: {_yaml_scalar(revisions['alignment'])}",
        f"diarization-model: {_yaml_scalar(revisions['diarization'])}",
        f"failed-phases: {_yaml_scalar(failed_phases)}",
        "---",
    ]
    display_names = transcript["speakers"]["display_names"]
    body = []
    for segment in transcript["segments"]:
        text = " ".join(str(segment["text"]).split())
        if not text:
            continue
        text = _with_terminal_punctuation(text)
        anonymous = segment.get("speaker")
        speaker = display_names.get(anonymous, anonymous) if anonymous else None
        speaker = speaker or "SPEAKER_UNKNOWN"
        body.append(
            f"[{_format_timestamp(segment['start_ms'])}–"
            f"{_format_timestamp(segment['end_ms'])}] **{speaker}:** {text}"
        )
    return "\n".join(frontmatter + ([""] + body if body else [])) + "\n"


def publish_transcript(
    directory: str | os.PathLike[str],
    transcript: Mapping[str, Any],
    *,
    basename: str = "transcript",
) -> dict[str, ArtifactReference]:
    root = Path(directory)
    json_reference = write_json_artifact(root / f"{basename}.json", transcript)
    markdown_reference = atomic_write_bytes(
        root / f"{basename}.md", render_markdown(transcript).encode()
    )
    return {"json": json_reference, "markdown": markdown_reference}


def _normalize_assignment(
    assignment: Mapping[str, Any] | None,
) -> dict[str, str | None]:
    assignment = assignment or {}
    course = assignment.get("course")
    class_date = assignment.get("class_date")
    if course is not None:
        course = " ".join(str(course).split())
        if not course or len(course) > 200 or _ABSOLUTE_PATH_RE.search(course):
            raise ValueError("course is invalid")
    if class_date is not None:
        class_date = str(class_date)
        if not _ISO_DATE_RE.fullmatch(class_date):
            raise ValueError("class_date must use YYYY-MM-DD")
        try:
            parsed_date = date.fromisoformat(class_date)
        except ValueError as error:
            raise ValueError("class_date must be a real calendar date") from error
        if parsed_date.isoformat() != class_date:
            raise ValueError("class_date must use YYYY-MM-DD")
    return {"course": course, "class_date": class_date}


def _safe_revision(value: str | None) -> str | None:
    if value is None:
        return None
    revision = " ".join(str(value).split())
    if not revision or len(revision) > 200 or _ABSOLUTE_PATH_RE.search(revision):
        raise ValueError("model revision is invalid")
    return revision


def _normalize_segments(
    segments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    indexed = list(enumerate(segments))
    indexed.sort(
        key=lambda item: (_milliseconds(item[1], "start", "start_ms"), item[0])
    )
    speaker_ids: dict[str, str] = {}

    def anonymize(value: Any) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        original = str(value)
        if original not in speaker_ids:
            speaker_ids[original] = f"SPEAKER_{len(speaker_ids):02d}"
        return speaker_ids[original]

    normalized = []
    for _, segment in indexed:
        start_ms = _milliseconds(segment, "start", "start_ms")
        end_ms = max(start_ms, _milliseconds(segment, "end", "end_ms"))
        segment_speaker = anonymize(segment.get("speaker"))
        words = []
        for word in segment.get("words") or []:
            word_start = _milliseconds(word, "start", "start_ms", nullable=True)
            word_end = _milliseconds(word, "end", "end_ms", nullable=True)
            if word_start is not None and word_end is not None:
                word_end = max(word_start, word_end)
            words.append(
                {
                    "start_ms": word_start,
                    "end_ms": word_end,
                    "text": str(word.get("word", word.get("text", ""))),
                    "speaker": anonymize(word.get("speaker")),
                }
            )
        normalized.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": str(segment.get("text", "")),
                "speaker": segment_speaker,
                "words": words,
            }
        )
    return normalized, list(speaker_ids.values())


def _milliseconds(
    value: Mapping[str, Any],
    seconds_key: str,
    milliseconds_key: str,
    *,
    nullable: bool = False,
) -> int | None:
    raw = value.get(milliseconds_key)
    multiplier = 1
    if raw is None:
        raw = value.get(seconds_key)
        multiplier = 1000
    if raw is None:
        if nullable:
            return None
        raise ValueError(f"segment is missing {seconds_key}")
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError(f"{seconds_key} must be finite")
    return max(0, int(round(number * multiplier)))


def _format_timestamp(milliseconds: int) -> str:
    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _with_terminal_punctuation(text: str) -> str:
    visible_end = text.rstrip("\"'’”)]}»")
    return text if visible_end.endswith((".", "?", "!", "…")) else f"{text}."


def _yaml_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
