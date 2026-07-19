from datetime import datetime, timezone

import pytest

from config import Config
from media import MediaItem, discover_local_media
from transcribe import TranscriptionResult, TranscriptionService


def test_mp3_is_discovered_and_sent_directly_to_whisper(tmp_path, monkeypatch):
    source = tmp_path / "class.mp3"
    source.write_bytes(b"audio")
    config = Config()
    items = discover_local_media([str(tmp_path)], True, config)
    assert [item.original_filename for item in items] == ["class.mp3"]

    service = TranscriptionService(config)
    monkeypatch.setattr(
        service,
        "extract_audio_from_video",
        lambda *_: pytest.fail("MP3 must not be extracted as video"),
    )
    monkeypatch.setattr(
        service,
        "transcribe_audio",
        lambda audio, output, **kwargs: TranscriptionResult(
            True, "text", str(output), output
        ),
    )
    result = service.process_media(source, tmp_path / "out")
    assert result.success


def test_media_fingerprint_carries_source_identity():
    item = MediaItem(
        "videos/class.mp3",
        "class.mp3",
        10,
        "s3",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        etag="abc",
    )
    assert "videos/class.mp3" in item.fingerprint
    assert "abc" in item.fingerprint
