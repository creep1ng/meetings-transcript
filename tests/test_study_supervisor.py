"""IPC and process lifecycle tests using the lightweight fake worker."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path

import pytest

from study.adapters.persistence.job_store import AcademicJobStore
from study.adapters.transcription.ipc import (
    ArtifactValidationError,
    MalformedMessageError,
    PipelineSignatureError,
    ProtocolVersionError,
    WorkerDiedError,
    encode_message,
)
from study.adapters.transcription.supervisor import WorkerSupervisor
from study.application.ports.jobs import JobStatus
from study.application.ports.transcriber import TranscriptionRequest
from study.application.use_cases.transcribe_media import (
    TranscribeMedia,
    TranscribeMediaCommand,
)
from study.worker.pipeline import PipelineConfig, WhisperXPipeline

FAKE_WORKER = Path(__file__).parent / "fixtures/fake_study_worker.py"
SIGNATURE = "a" * 64


def command(*args: str) -> tuple[str, ...]:
    return (sys.executable, str(FAKE_WORKER), *args)


def request(tmp_path: Path, job_id: str, mode: str = "normal") -> TranscriptionRequest:
    staging = tmp_path / job_id
    staging.mkdir()
    return TranscriptionRequest(
        job_id=job_id,
        source_path=str(tmp_path / f"{mode}.mp3"),
        source_sha256="1" * 64,
        pipeline_signature=SIGNATURE,
        staging_path=str(staging),
        options={"media_kind": "audio", "language": "es"},
    )


def test_worker_is_lazy_persistent_and_uses_small_artifact_events(tmp_path: Path):
    events = []
    logs = []
    with WorkerSupervisor(
        SIGNATURE, command(), stderr_handler=logs.append
    ) as supervisor:
        assert supervisor.command[0] == sys.executable
        assert supervisor.pid is None
        first = supervisor.submit(request(tmp_path, "one"), events.append).result(5)
        pid = supervisor.pid
        second = supervisor.submit(request(tmp_path, "two"), events.append).result(5)

        assert first.status is JobStatus.COMPLETED
        assert second.completed_phases == ("asr", "alignment", "diarization")
        assert supervisor.pid == pid
        assert any(event.kind == "checkpoint" for event in events)
        assert any("fake worker log" in line for line in logs)


def test_default_worker_completes_same_environment_handshake():
    signature = WhisperXPipeline(PipelineConfig()).signature
    with WorkerSupervisor(signature) as supervisor:
        process = supervisor._ensure_worker()
        assert process.pid == supervisor.pid


def test_ipc_rejects_inline_result_payloads():
    with pytest.raises(MalformedMessageError, match="unexpected fields"):
        encode_message(
            {
                "type": "response",
                "id": "job",
                "ok": True,
                "result": {
                    "status": "complete",
                    "assigned": False,
                    "completed_phases": [],
                    "failed_phases": [],
                    "error": None,
                    "pipeline_signature": SIGNATURE,
                    "profile": {"device": "cpu", "compute_type": "int8"},
                    "artifacts": {},
                    "text": "transcript content must stay on disk",
                },
            }
        )


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("bad-version", ProtocolVersionError),
        ("bad-signature", PipelineSignatureError),
        ("malformed", MalformedMessageError),
        ("eof", WorkerDiedError),
    ],
)
def test_invalid_handshake_fails_without_idle_restart(
    tmp_path: Path, mode: str, error: type[Exception]
):
    with WorkerSupervisor(SIGNATURE, command("--handshake", mode)) as supervisor:
        with pytest.raises(error):
            supervisor.submit(request(tmp_path, "job")).result(5)
        time.sleep(0.05)
        assert supervisor.pid is None


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("stdout-noise", MalformedMessageError),
        ("malformed", MalformedMessageError),
        ("eof", WorkerDiedError),
        ("die", WorkerDiedError),
        ("corrupt-artifact", ArtifactValidationError),
    ],
)
def test_bad_job_stream_fails_and_stops_worker(
    tmp_path: Path, mode: str, error: type[Exception]
):
    with WorkerSupervisor(SIGNATURE, command()) as supervisor:
        with pytest.raises(error):
            supervisor.submit(request(tmp_path, "job", mode)).result(5)
        time.sleep(0.05)
        assert supervisor.pid is None


def test_partial_result_identifies_retry_phase(tmp_path: Path):
    with WorkerSupervisor(SIGNATURE, command()) as supervisor:
        outcome = supervisor.submit(request(tmp_path, "job", "partial")).result(5)
        assert outcome.status is JobStatus.PARTIAL
        assert outcome.completed_phases == ("asr",)
        assert outcome.failed_phase == "alignment"


def test_confirmed_phase_checkpoint_survives_worker_death(tmp_path: Path):
    events = []
    with WorkerSupervisor(SIGNATURE, command()) as supervisor:
        with pytest.raises(WorkerDiedError):
            supervisor.submit(
                request(tmp_path, "job", "die-after-asr"), events.append
            ).result(5)

    checkpoint = next(event for event in events if event.kind == "checkpoint")
    assert checkpoint.phase == "asr"
    assert checkpoint.artifact is not None
    assert Path(checkpoint.artifact.path).is_file()


def test_use_case_persists_supervised_worker_artifacts(tmp_path: Path):
    source = tmp_path / "normal.mp3"
    source.write_bytes(b"audio")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    with (
        AcademicJobStore(tmp_path / "state") as store,
        WorkerSupervisor(SIGNATURE, command()) as supervisor,
    ):
        submitted = TranscribeMedia(store, supervisor).execute(
            TranscribeMediaCommand(
                source_path=str(source),
                source_sha256=source_sha256,
                pipeline_signature=SIGNATURE,
            )
        )
        assert submitted.future.result(5).status is JobStatus.COMPLETED
        deadline = time.monotonic() + 2
        while store.get_job(submitted.job.id).status is JobStatus.RUNNING:  # type: ignore[union-attr]
            assert time.monotonic() < deadline
            time.sleep(0.01)

        persisted = store.get_job(submitted.job.id)
        assert persisted is not None
        assert persisted.status is JobStatus.UNASSIGNED
        assert {checkpoint.phase for checkpoint in persisted.checkpoints} >= {
            "asr",
            "alignment",
            "diarization",
            "transcript_json",
            "transcript_markdown",
        }


def test_cancel_terminates_group_and_restarts_only_for_queued_work(tmp_path: Path):
    started = threading.Event()
    with WorkerSupervisor(SIGNATURE, command(), terminate_timeout=0.1) as supervisor:
        hanging = supervisor.submit(
            request(tmp_path, "hanging", "hang-with-child"),
            lambda event: started.set() if event.kind == "progress" else None,
        )
        assert started.wait(5)
        old_pid = supervisor.pid
        child_pid = int((tmp_path / "hanging/child.pid").read_text())

        assert supervisor.cancel("hanging")
        with pytest.raises(CancelledError):
            hanging.result()
        time.sleep(0.1)
        assert supervisor.pid is None
        assert old_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)

        completed = supervisor.submit(request(tmp_path, "next")).result(5)
        assert completed.status is JobStatus.COMPLETED
        assert supervisor.pid != old_pid
