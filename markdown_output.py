"""Deterministic Obsidian Markdown generation and media date probing."""

import json
import logging
import subprocess
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

from media import MediaItem
from transcribe import TranscriptionResult

logger = logging.getLogger(__name__)


def probe_recorded_at(media_path: Path) -> Optional[datetime]:
    """Read a reliable embedded recording date without using file mtime."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags=creation_time,date:stream_tags=creation_time,date",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.debug("Embedded recording date unavailable for %s: %s", media_path, exc)
        return None

    candidates: list[str] = []
    candidates.extend(_tag_values(payload.get("format", {}).get("tags", {})))
    for stream in payload.get("streams", []):
        candidates.extend(_tag_values(stream.get("tags", {})))

    for candidate in candidates:
        parsed = _parse_embedded_date(candidate)
        if parsed is not None:
            return parsed
    logger.debug("No reliable embedded recording date found for %s", media_path)
    return None


def format_obsidian_markdown(
    item: MediaItem,
    result: TranscriptionResult,
    model: str,
    transcribed_at: datetime,
    recorded_at: Optional[datetime] = None,
) -> str:
    """Format a transcript as deterministic UTF-8 Obsidian Markdown."""
    fields: list[tuple[str, Any]] = [
        ("source_file", item.original_filename),
        ("source_type", item.source_type),
        ("transcribed_at", _iso(transcribed_at)),
        ("model", model),
        ("duration_seconds", round(result.duration_seconds or 0, 3)),
    ]
    if recorded_at is not None:
        fields.append(("recorded_at", _iso(recorded_at)))
    fields.append(("source_modified_at", _iso(item.source_modified_at)))

    frontmatter = ["---"]
    for key, value in fields:
        serialized = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, str)
            else str(value)
        )
        frontmatter.append(f"{key}: {serialized}")
    frontmatter.extend(
        ["---", "", f"# {item.original_filename}", "", result.text.strip(), ""]
    )
    return "\n".join(frontmatter)


def _tag_values(tags: dict[str, Any]) -> list[str]:
    lowered = {str(key).lower(): value for key, value in tags.items()}
    return [str(lowered[key]) for key in ("creation_time", "date") if lowered.get(key)]


def _parse_embedded_date(value: str) -> Optional[datetime]:
    normalized = value.strip()
    if len(normalized) == 4 and normalized.isdigit():
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            date_value = datetime.strptime(normalized, "%Y-%m-%d").date()
        except ValueError:
            return None
        parsed = datetime.combine(date_value, time.min, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
