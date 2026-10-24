import copy
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from study.adapters.transcription.ipc import validate_message
from study.worker import pipeline as pipeline_module
from study.worker.__main__ import run
from study.worker.pipeline import PipelineConfig, WhisperXPipeline


class FakeASR:
    def __init__(self, owner):
        self.owner = owner

    def transcribe(self, audio, **options):
        self.owner.transcriptions += 1
        self.owner.transcribe_options.append(options)
        if self.owner.fail_asr:
            raise RuntimeError("ASR cache /home/alice token=hf_abcdefgh")
        return {
            "language": "es",
            "segments": [{"start": 0.1, "end": 2.8, "text": "Hola clase"}],
        }


class FakeDiarizer:
    def __init__(self, owner):
        self.owner = owner

    def __call__(self, audio, **options):
        self.owner.diarization_options.append(options)
        if self.owner.fail_diarization:
            raise RuntimeError(
                "Diarization /home/alice/models username alice secret=top-secret"
            )
        return [{"start": 0.0, "end": 3.0, "speaker": "teacher"}]


class FakeWhisperX:
    def __init__(self):
        self.asr_loads = 0
        self.alignment_loads = 0
        self.diarization_loads = 0
        self.transcriptions = 0
        self.load_audio_paths = []
        self.load_model_options = []
        self.alignment_options = []
        self.diarization_model_options = []
        self.transcribe_options = []
        self.diarization_options = []
        self.fail_asr = False
        self.fail_alignment = False
        self.fail_diarization = False

    def load_audio(self, path):
        self.load_audio_paths.append(path)
        return f"audio:{path}"

    def load_model(self, model, device, **options):
        self.asr_loads += 1
        self.load_model_options.append((model, device, options))
        return FakeASR(self)

    def load_align_model(self, **options):
        self.alignment_loads += 1
        self.alignment_options.append(options)
        return object(), {"language": options["language_code"]}

    def align(self, segments, model, metadata, audio, device, **options):
        if self.fail_alignment:
            raise RuntimeError(
                "Alignment /home/alice/models token=hf_abcdefgh username alice"
            )
        aligned = copy.deepcopy(segments)
        aligned[0]["words"] = [
            {"word": "Hola", "start": 0.1, "end": 0.5},
            {"word": "clase", "start": 0.6, "end": 1.0},
        ]
        return {"segments": aligned}

    def DiarizationPipeline(self, **options):
        self.diarization_loads += 1
        self.diarization_model_options.append(options)
        return FakeDiarizer(self)

    def assign_word_speakers(self, diarized, result, **options):
        assigned = copy.deepcopy(result)
        for segment in assigned["segments"]:
            segment["speaker"] = "teacher"
            for word in segment.get("words", []):
                word["speaker"] = "teacher"
        return assigned


@pytest.fixture
def fake_media_tools(monkeypatch):
    calls = []

    def fake_run(command, **options):
        calls.append((command, options))
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout="12.345\n")
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"extracted wav")
            return SimpleNamespace(stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected command: {command[0]}")

    monkeypatch.setattr(pipeline_module.subprocess, "run", fake_run)
    return calls


def request_for(source, staging, media_kind="audio"):
    return {
        "source_path": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "staging_dir": str(staging),
        "media_kind": media_kind,
        "language": "es",
        "assignment": {"course": "Systems", "class_date": "2026-08-17"},
    }


def pipeline_with(fake_whisperx, monkeypatch, **config):
    pipeline = WhisperXPipeline(
        PipelineConfig(device="cpu", compute_type="auto", **config)
    )
    monkeypatch.setattr(pipeline, "_import_whisperx", lambda: fake_whisperx)
    return pipeline


def test_pipeline_module_does_not_import_whisperx_eagerly():
    assert "whisperx" not in sys.modules


@pytest.mark.parametrize(
    ("cuda_available", "expected"),
    [(True, ("cuda", "float16")), (False, ("cpu", "int8"))],
)
def test_auto_profile_uses_cuda_float16_or_cpu_int8(
    monkeypatch, cuda_available, expected
):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda_available)
    )
    monkeypatch.setattr(
        pipeline_module.importlib,
        "import_module",
        lambda name: (
            fake_torch if name == "torch" else pytest.fail(f"unexpected import: {name}")
        ),
    )

    pipeline = WhisperXPipeline(PipelineConfig(device="auto", compute_type="auto"))

    assert pipeline._resolve_profile() == expected


def test_diarization_pipeline_supports_whisperx_submodule_export(monkeypatch):
    owner = FakeWhisperX()
    root_without_export = SimpleNamespace()
    module = SimpleNamespace(DiarizationPipeline=owner.DiarizationPipeline)
    pipeline = WhisperXPipeline(PipelineConfig(device="cpu", compute_type="int8"))
    monkeypatch.setattr(
        pipeline_module.importlib,
        "import_module",
        lambda name: (
            module
            if name == "whisperx.diarize"
            else pytest.fail(f"unexpected import: {name}")
        ),
    )

    loaded = pipeline._get_diarization_model(root_without_export, "cpu")

    assert isinstance(loaded, FakeDiarizer)
    assert owner.diarization_loads == 1


def test_pipeline_reuses_models_and_writes_hashed_phase_artifacts(
    tmp_path, monkeypatch, fake_media_tools
):
    source = tmp_path / "class.m4a"
    source.write_bytes(b"audio source")
    fake = FakeWhisperX()
    pipeline = pipeline_with(fake, monkeypatch)
    events = []

    first = pipeline.transcribe(request_for(source, tmp_path / "first"), events.append)
    second = pipeline.transcribe(
        request_for(source, tmp_path / "second", "video"), events.append
    )

    assert first["status"] == second["status"] == "complete"
    assert first["profile"] == {"device": "cpu", "compute_type": "int8"}
    assert fake.asr_loads == 1
    assert fake.alignment_loads == 1
    assert fake.diarization_loads == 1
    assert fake.transcriptions == 2
    assert fake.load_model_options[0][0:2] == ("large-v3", "cpu")
    assert fake.load_model_options[0][2]["local_files_only"] is True
    assert fake.alignment_options[0]["language_code"] == "es"
    assert fake.alignment_options[0]["model_cache_only"] is True
    assert fake.diarization_model_options[0]["model_name"] == (
        "pyannote/speaker-diarization-community-1"
    )
    assert Path(fake.load_audio_paths[1]).name == "audio.wav"
    assert any(command[0] == "ffprobe" for command, _ in fake_media_tools)
    assert any(command[0] == "ffmpeg" for command, _ in fake_media_tools)
    assert "audio" not in first["artifacts"]
    assert "audio" in second["artifacts"]
    for reference in second["artifacts"].values():
        assert set(reference) == {"path", "sha256"}
        assert Path(reference["path"]).is_file()
        assert len(reference["sha256"]) == 64
    transcript = json.loads(
        Path(second["artifacts"]["transcript_json"]["path"]).read_text()
    )
    assert transcript["source"]["duration_ms"] == 12_345
    assert transcript["speakers"]["anonymous_ids"] == ["SPEAKER_00"]
    assert events.count({"phase": "asr", "state": "started"}) == 2


def test_alignment_and_diarization_failures_publish_partial_transcript(
    tmp_path, monkeypatch, fake_media_tools
):
    source = tmp_path / "class.wav"
    source.write_bytes(b"audio source")
    fake = FakeWhisperX()
    fake.fail_alignment = True
    fake.fail_diarization = True
    pipeline = pipeline_with(fake, monkeypatch)

    result = pipeline.transcribe(request_for(source, tmp_path / "partial"))
    transcript = json.loads(
        Path(result["artifacts"]["transcript_json"]["path"]).read_text()
    )

    assert result["status"] == "partial"
    assert transcript["phases"] == {
        "alignment": "failed",
        "asr": "completed",
        "diarization": "failed",
    }
    assert [error["phase"] for error in transcript["errors"]] == [
        "alignment",
        "diarization",
    ]
    serialized = json.dumps(transcript)
    assert "/home/alice" not in serialized
    assert "hf_abcdefgh" not in serialized
    assert "username alice" not in serialized
    assert transcript["segments"][0]["speaker"] is None
    assert set(result["artifacts"]) >= {
        "asr",
        "alignment",
        "diarization",
        "transcript_json",
        "transcript_markdown",
    }


def test_retry_from_alignment_reuses_valid_asr_checkpoint(
    tmp_path, monkeypatch, fake_media_tools
):
    source = tmp_path / "class.wav"
    source.write_bytes(b"audio source")
    fake = FakeWhisperX()
    fake.fail_alignment = True
    pipeline = pipeline_with(fake, monkeypatch)
    staging = tmp_path / "retry"

    first = pipeline.transcribe(request_for(source, staging))
    fake.fail_alignment = False
    request = request_for(source, staging)
    request["start_phase"] = "alignment"
    request["checkpoints"] = [
        {
            "phase": "asr",
            "path": first["artifacts"]["asr"]["path"],
            "sha256": first["artifacts"]["asr"]["sha256"],
        }
    ]

    retried = pipeline.transcribe(request)

    assert retried["status"] == "complete"
    assert fake.transcriptions == 1
    assert retried["completed_phases"] == ["asr", "alignment", "diarization"]


def test_video_extraction_uses_atomic_staging_file(
    tmp_path, monkeypatch, fake_media_tools
):
    source = tmp_path / "class.mp4"
    source.write_bytes(b"video source")
    fake = FakeWhisperX()
    pipeline = pipeline_with(fake, monkeypatch)

    result = pipeline.transcribe(request_for(source, tmp_path / "video-job", "video"))

    audio = Path(result["artifacts"]["audio"]["path"])
    assert audio.name == "audio.wav"
    assert audio.read_bytes() == b"extracted wav"
    assert not list(audio.parent.glob(".audio.*.wav"))


def test_worker_ndjson_handshake_progress_and_stdout_isolation():
    class FakePipeline:
        signature = "c" * 64

        def transcribe(self, params, progress):
            print("third-party model noise")
            progress({"phase": "asr", "state": "started"})
            return {
                "status": "complete",
                "assigned": True,
                "completed_phases": ["asr", "alignment", "diarization"],
                "failed_phases": [],
                "error": None,
                "artifacts": {
                    "transcript_json": {"path": "/staging/t.json", "sha256": "d" * 64}
                },
            }

    requests = [
        {
            "type": "request",
            "protocol_version": 1,
            "id": "ping-1",
            "method": "ping",
            "params": {},
        },
        {
            "type": "request",
            "protocol_version": 1,
            "id": "job-1",
            "method": "transcribe",
            "params": {"narrow": "pipeline-owned"},
        },
        {
            "type": "request",
            "protocol_version": 1,
            "id": "stop-1",
            "method": "shutdown",
            "params": {},
        },
    ]
    stdin = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert run(stdin, stdout, stderr, pipeline=FakePipeline()) == 0

    messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert all(validate_message(message) == message for message in messages)
    assert messages[0]["type"] == "hello"
    assert messages[0]["protocol_version"] == 1
    assert messages[0]["requests"] == ["ping", "shutdown", "transcribe"]
    assert messages[1]["result"]["state"] == "ready"
    assert messages[2] == {
        "event": "phase",
        "id": "job-1",
        "phase": "asr",
        "state": "started",
        "type": "event",
    }
    assert messages[3]["result"]["artifacts"]["transcript_json"] == {
        "path": "/staging/t.json",
        "sha256": "d" * 64,
    }
    assert messages[4]["result"] == {"state": "stopping"}
    assert "third-party model noise" not in stdout.getvalue()
    assert "third-party model noise" in stderr.getvalue()


def test_worker_rejects_malformed_ndjson_without_crashing():
    fake_pipeline = SimpleNamespace(signature="e" * 64)
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert run(io.StringIO("not-json\n"), stdout, stderr, pipeline=fake_pipeline) == 0

    messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert messages[1]["ok"] is False
    assert messages[1]["error"]["code"] == "invalid_request"
    assert "invalid request" in stderr.getvalue()


@pytest.mark.skipif(
    os.environ.get("RUN_WHISPERX_SMOKE") != "1",
    reason="set RUN_WHISPERX_SMOKE=1 to use provisioned local models",
)
def test_real_whisperx_cpu_smoke_uses_cached_models_only(tmp_path):
    media = os.environ.get("WHISPERX_SMOKE_MEDIA")
    if not media or not Path(media).is_file():
        pytest.skip("WHISPERX_SMOKE_MEDIA must point to a local media file")
    source = Path(media)
    pipeline = WhisperXPipeline(
        PipelineConfig(
            device="cpu",
            compute_type="int8",
            local_files_only=True,
            model_cache=os.environ.get("ACADEMIC_MODEL_CACHE"),
        )
    )

    result = pipeline.transcribe(request_for(source, tmp_path / "smoke"))

    assert result["status"] in {"complete", "partial"}
