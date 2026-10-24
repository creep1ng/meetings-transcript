"""Durability and invalidation tests for academic transcription jobs."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from study.adapters.persistence.job_store import (
    AcademicJobStore,
    ArtifactValidationError,
    InvalidatedJobError,
)
from study.application.ports.jobs import JobCheckpoint, JobStatus
from study.application.use_cases.recover_jobs import RecoverJobs


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def source_file(tmp_path: Path, content: bytes = b"class audio") -> Path:
    source = tmp_path / "class.mp3"
    source.write_bytes(content)
    return source


def test_job_store_uses_wal_outside_library_and_content_identity(tmp_path: Path):
    library = tmp_path / "library"
    library.mkdir()
    source = source_file(tmp_path)
    with AcademicJobStore(tmp_path / "state", library_root=library) as store:
        first = store.get_or_create(
            str(source), digest(source.read_bytes()), "pipeline-a"
        )
        same = store.get_or_create(
            str(source), digest(source.read_bytes()), "pipeline-a"
        )

        assert first.id == same.id
        assert len(first.id) == 64
        with sqlite3.connect(store.db_path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    with pytest.raises(ValueError, match="outside the library"):
        AcademicJobStore(library / "state", library_root=library)


def test_library_environment_also_guards_default_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("ACADEMIC_LIBRARY_ROOT", str(library))
    with pytest.raises(ValueError, match="outside the library"):
        AcademicJobStore(library / "state")


def test_hash_or_pipeline_change_invalidates_old_job(tmp_path: Path):
    source = source_file(tmp_path, b"one")
    with AcademicJobStore(tmp_path / "state") as store:
        original = store.get_or_create(str(source), digest(b"one"), "pipeline-a")
        changed_pipeline = store.get_or_create(
            str(source), digest(b"one"), "pipeline-b"
        )
        stale = store.get_job(original.id)

        assert changed_pipeline.id != original.id
        assert stale is not None and stale.invalidated
        assert stale.status is JobStatus.RECOVERABLE
        with pytest.raises(InvalidatedJobError):
            store.retry(original.id)

        source.write_bytes(b"two")
        changed_source = store.get_or_create(str(source), digest(b"two"), "pipeline-b")
        assert changed_source.id != changed_pipeline.id
        assert store.get_job(changed_pipeline.id).invalidated  # type: ignore[union-attr]


def test_equal_content_at_different_paths_keeps_distinct_assignments(tmp_path: Path):
    first_source = source_file(tmp_path, b"same")
    second_source = tmp_path / "copy.mp3"
    second_source.write_bytes(b"same")
    with AcademicJobStore(tmp_path / "state") as store:
        first = store.get_or_create(str(first_source), digest(b"same"), "pipeline")
        second = store.get_or_create(str(second_source), digest(b"same"), "pipeline")
        store.set_assignment(first.id, "Algorithms", "2026-08-17")
        store.set_assignment(second.id, "Databases", "2026-08-18")

        assert first.id != second.id
        assert store.get_job(first.id).course == "Algorithms"  # type: ignore[union-attr]
        assert store.get_job(second.id).course == "Databases"  # type: ignore[union-attr]


def test_phase_checkpoints_are_validated_and_retry_starts_at_failure(tmp_path: Path):
    source = source_file(tmp_path)
    with AcademicJobStore(tmp_path / "state") as store:
        job = store.get_or_create(str(source), digest(source.read_bytes()), "pipeline")
        artifact = Path(job.staging_path) / "asr.json"
        artifact.write_bytes(b"asr")
        checkpoint = store.record_checkpoint(
            job.id,
            JobCheckpoint("asr", str(artifact), digest(b"asr")),
        )
        store.mark_status(
            job.id,
            JobStatus.PARTIAL,
            failed_phase="alignment",
            error="alignment unavailable",
        )

        assert store.resume_checkpoints(job.id) == (checkpoint,)
        retried = store.retry(job.id)
        assert retried.status is JobStatus.PENDING
        assert retried.current_phase == "alignment"

        artifact.write_bytes(b"mutated")
        assert store.resume_checkpoints(job.id) == ()
        recovered = store.get_job(job.id)
        assert recovered is not None
        assert recovered.status is JobStatus.RECOVERABLE
        assert recovered.failed_phase == "asr"


def test_checkpoint_must_be_inside_staging_with_matching_sha(tmp_path: Path):
    source = source_file(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"artifact")
    with AcademicJobStore(tmp_path / "state") as store:
        job = store.get_or_create(str(source), digest(source.read_bytes()), "pipeline")
        with pytest.raises(ArtifactValidationError, match="outside"):
            store.record_checkpoint(
                job.id,
                JobCheckpoint("asr", str(outside), digest(outside.read_bytes())),
            )

        inside = Path(job.staging_path) / "inside.json"
        inside.write_bytes(b"artifact")
        with pytest.raises(ArtifactValidationError, match="hash mismatch"):
            store.record_checkpoint(job.id, JobCheckpoint("asr", str(inside), "0" * 64))


def test_recovery_reports_disk_usage_and_discard_removes_state(tmp_path: Path):
    source = source_file(tmp_path)
    with AcademicJobStore(tmp_path / "state") as store:
        job = store.get_or_create(str(source), digest(source.read_bytes()), "pipeline")
        retained = Path(job.staging_path) / "retained.bin"
        retained.write_bytes(b"123456")
        store.mark_running(job.id, "diarization")

        snapshot = RecoverJobs(store).execute()
        assert snapshot.total_staging_bytes == 6
        assert snapshot.jobs[0].job.status is JobStatus.RECOVERABLE
        assert snapshot.jobs[0].retry_phase == "diarization"
        assert RecoverJobs(store).discard(job.id) == 6
        assert store.get_job(job.id) is None
        assert not Path(job.staging_path).exists()


def test_complete_removes_staging_but_keeps_audit_record(tmp_path: Path):
    source = source_file(tmp_path)
    with AcademicJobStore(tmp_path / "state") as store:
        job = store.get_or_create(str(source), digest(source.read_bytes()), "pipeline")
        retained = Path(job.staging_path) / "published.bin"
        retained.write_bytes(b"published")

        assert store.complete(job.id) == len(b"published")
        completed = store.get_job(job.id)
        assert completed is not None
        assert completed.status is JobStatus.COMPLETED
        assert completed.checkpoints == ()
        assert not Path(job.staging_path).exists()


def test_source_mutation_prevents_checkpoint_reuse(tmp_path: Path):
    source = source_file(tmp_path, b"original")
    with AcademicJobStore(tmp_path / "state") as store:
        job = store.get_or_create(str(source), digest(b"original"), "pipeline")
        artifact = Path(job.staging_path) / "asr.json"
        artifact.write_bytes(b"checkpoint")
        store.record_checkpoint(
            job.id,
            JobCheckpoint("asr", str(artifact), digest(b"checkpoint")),
        )
        source.write_bytes(b"changed")

        assert store.resume_checkpoints(job.id) == ()
        stale = store.get_job(job.id)
        assert stale is not None and stale.invalidated
