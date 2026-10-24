import json
from pathlib import Path

import pytest

from study.adapters.transcription.artifacts import (
    build_transcript,
    publish_transcript,
    render_markdown,
    sha256_file,
)
from study.application.use_cases.rename_speakers import rename_speakers


def transcript_fixture():
    return build_transcript(
        source_sha256="a" * 64,
        duration_ms=742_000,
        assignment={"course": "Architecture", "class_date": "2026-08-17"},
        requested_language="es",
        detected_language="es",
        revisions={
            "pipeline": "whisperx-worker-v1",
            "asr": "large-v3",
            "alignment": "default:es",
            "diarization": "pyannote/speaker-diarization-community-1",
        },
        phases={
            "asr": "completed",
            "alignment": "completed",
            "diarization": "completed",
        },
        errors=[],
        segments=[
            {
                "start": 728.2,
                "end": 741.9,
                "text": "Transcript text",
                "speaker": "raw-teacher-label",
                "words": [
                    {
                        "word": "Transcript",
                        "start": None,
                        "end": None,
                        "speaker": "raw-teacher-label",
                    }
                ],
            }
        ],
    )


def test_canonical_transcript_uses_milliseconds_and_anonymous_speakers():
    transcript = transcript_fixture()

    assert transcript["version"] == 1
    assert transcript["status"] == "complete"
    assert transcript["segments"][0] == {
        "start_ms": 728_200,
        "end_ms": 741_900,
        "text": "Transcript text",
        "speaker": "SPEAKER_00",
        "words": [
            {
                "start_ms": None,
                "end_ms": None,
                "text": "Transcript",
                "speaker": "SPEAKER_00",
            }
        ],
    }
    assert transcript["speakers"] == {
        "anonymous_ids": ["SPEAKER_00"],
        "display_names": {"SPEAKER_00": "SPEAKER_00"},
    }
    serialized = json.dumps(transcript)
    assert "raw-teacher-label" not in serialized
    assert "/home/" not in serialized


def test_partial_errors_are_sanitized():
    transcript = build_transcript(
        source_sha256="b" * 64,
        duration_ms=1_000,
        assignment=None,
        requested_language="auto",
        detected_language="en",
        revisions={
            "pipeline": "worker-v1",
            "asr": "large-v3",
            "alignment": None,
            "diarization": None,
        },
        phases={
            "asr": "completed",
            "alignment": "failed",
            "diarization": "failed",
        },
        errors=[
            {
                "phase": "alignment",
                "message": (
                    "cache /home/alice/models token=hf_abcdefgh username alice"
                ),
            }
        ],
        segments=[{"start": 0, "end": 1, "text": "Still useful"}],
    )

    assert transcript["status"] == "partial"
    error = transcript["errors"][0]["message"]
    assert "<path>" in error
    assert "<redacted>" in error
    assert "/home/alice" not in error
    assert "hf_abcdefgh" not in error
    assert "username alice" not in error


def test_markdown_is_deterministic_and_uses_display_names():
    transcript = transcript_fixture()

    expected_line = "[00:12:08–00:12:21] **SPEAKER_00:** Transcript text."
    first = render_markdown(transcript)
    second = render_markdown(transcript)

    assert first == second
    assert first.startswith("---\ntranscript-version: 1\n")
    assert "failed-phases: []\n---\n\n" in first
    assert expected_line in first


def test_rename_updates_only_display_map_and_regenerates_markdown():
    original = transcript_fixture()

    result = rename_speakers(
        original,
        {"SPEAKER_00": "Professor Vega"},
        render_markdown=render_markdown,
    )

    assert original["speakers"]["display_names"]["SPEAKER_00"] == "SPEAKER_00"
    assert result.transcript["segments"][0]["speaker"] == "SPEAKER_00"
    assert result.transcript["speakers"]["display_names"] == {
        "SPEAKER_00": "Professor Vega"
    }
    assert "**Professor Vega:** Transcript text." in result.markdown


def test_rename_rejects_unknown_speaker():
    with pytest.raises(ValueError, match="unknown anonymous speaker"):
        rename_speakers(
            transcript_fixture(),
            {"SPEAKER_99": "Unknown"},
            render_markdown=render_markdown,
        )


def test_transcript_publication_is_atomic_and_hash_addressable(tmp_path):
    references = publish_transcript(tmp_path, transcript_fixture())

    assert set(references) == {"json", "markdown"}
    for reference in references.values():
        path = Path(reference.path)
        assert path.is_file()
        assert reference.sha256 == sha256_file(path)
    assert not list(tmp_path.glob(".*.tmp"))
    assert json.loads((tmp_path / "transcript.json").read_text())["version"] == 1


def test_transcript_schema_declares_closed_v1_contract():
    schema_path = (
        Path(__file__).parents[1]
        / "study"
        / "contracts"
        / "schemas"
        / "transcript.schema.json"
    )
    schema = json.loads(schema_path.read_text())

    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["version"] == {"const": 1}
    word = schema["$defs"]["word"]
    assert word["properties"]["start_ms"]["$ref"].endswith("nullableMilliseconds")
