from datetime import datetime, timezone

import json
from types import SimpleNamespace

from markdown_output import format_obsidian_markdown, probe_recorded_at
from media import MediaItem
from transcribe import TranscriptionResult


def test_markdown_preserves_full_filename_and_escapes_yaml():
    item = MediaItem(
        source_id='/tmp/team "sync".final.mp3',
        original_filename='team "sync".final.mp3',
        size_bytes=42,
        source_type="local",
        source_modified_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    result = TranscriptionResult(True, "Hello world", "out", duration_seconds=1.23456)
    rendered = format_obsidian_markdown(
        item,
        result,
        "small",
        datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
    )
    assert 'source_file: "team \\"sync\\".final.mp3"' in rendered
    assert "recorded_at:" not in rendered
    assert "duration_seconds: 1.235" in rendered
    assert '# team "sync".final.mp3' in rendered
    assert rendered.endswith("Hello world\n")


def test_markdown_emits_recorded_at_separately():
    item = MediaItem(
        "s3-key",
        "class.mp4",
        1,
        "s3",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    rendered = format_obsidian_markdown(
        item,
        TranscriptionResult(True, "Text", "out"),
        "base",
        datetime(2026, 1, 3, tzinfo=timezone.utc),
        datetime(2025, 12, 1, tzinfo=timezone.utc),
    )
    assert 'recorded_at: "2025-12-01T00:00:00Z"' in rendered
    assert 'source_modified_at: "2026-01-01T00:00:00Z"' in rendered


def test_probe_ignores_ambiguous_year_only_tag(tmp_path, monkeypatch):
    payload = {"format": {"tags": {"date": "2026"}}, "streams": []}
    monkeypatch.setattr(
        "markdown_output.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    assert probe_recorded_at(tmp_path / "class.mp3") is None


def test_probe_parses_timezone_aware_creation_time(tmp_path, monkeypatch):
    payload = {
        "format": {"tags": {"creation_time": "2026-07-01T12:30:00Z"}},
        "streams": [],
    }
    monkeypatch.setattr(
        "markdown_output.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    assert probe_recorded_at(tmp_path / "class.mp4").isoformat() == (
        "2026-07-01T12:30:00+00:00"
    )
