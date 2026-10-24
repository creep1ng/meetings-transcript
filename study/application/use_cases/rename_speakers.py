"""Rename anonymous speakers without changing canonical segment identities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class RenameSpeakersResult:
    transcript: dict[str, Any]
    markdown: str


def rename_speakers(
    transcript: Mapping[str, Any],
    names: Mapping[str, str],
    *,
    render_markdown: Callable[[Mapping[str, Any]], str],
) -> RenameSpeakersResult:
    """Update the display-name map and regenerate Markdown through an injected port."""

    renamed = deepcopy(dict(transcript))
    speakers = renamed.get("speakers")
    if not isinstance(speakers, dict):
        raise ValueError("transcript has no speaker map")
    anonymous_ids = set(speakers.get("anonymous_ids", []))
    unknown = sorted(set(names) - anonymous_ids)
    if unknown:
        raise ValueError(f"unknown anonymous speaker: {unknown[0]}")

    display_names = dict(speakers.get("display_names", {}))
    for anonymous_id, display_name in names.items():
        normalized = " ".join(str(display_name).split())
        if not normalized or len(normalized) > 80:
            raise ValueError("display name must contain 1 to 80 characters")
        display_names[anonymous_id] = normalized
    speakers["display_names"] = {
        anonymous_id: display_names.get(anonymous_id, anonymous_id)
        for anonymous_id in speakers["anonymous_ids"]
    }
    return RenameSpeakersResult(renamed, render_markdown(renamed))
