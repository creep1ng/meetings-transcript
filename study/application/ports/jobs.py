"""Persistence boundary for recoverable academic transcription jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Collection, Protocol


class JobStatus(StrEnum):
    """Durable lifecycle states exposed to the application and presentation."""

    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNASSIGNED = "unassigned"
    RECOVERABLE = "recoverable"
    READY_TO_COMMIT = "ready_to_commit"
    COMPLETED = "completed"


RECOVERABLE_STATUSES = frozenset(
    {
        JobStatus.PENDING,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.UNASSIGNED,
        JobStatus.RECOVERABLE,
        JobStatus.READY_TO_COMMIT,
    }
)


@dataclass(frozen=True, slots=True)
class JobCheckpoint:
    """A small durable reference to one phase artifact."""

    phase: str
    artifact_path: str
    artifact_sha256: str
    sequence: int = 0
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class AcademicJob:
    """Application-safe projection of a persisted academic job."""

    id: str
    source_path: str
    source_sha256: str
    pipeline_signature: str
    status: JobStatus
    staging_path: str
    current_phase: str | None = None
    failed_phase: str | None = None
    error: str | None = None
    assigned: bool = False
    course: str | None = None
    class_date: str | None = None
    invalidated: bool = False
    invalidation_reason: str | None = None
    checkpoints: tuple[JobCheckpoint, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def retry_phase(self) -> str:
        """Resume at the failed/current phase, never after it."""

        phase = self.failed_phase or self.current_phase
        return phase if phase in {"asr", "alignment", "diarization"} else "asr"

    @property
    def is_recoverable(self) -> bool:
        return self.invalidated or self.status in RECOVERABLE_STATUSES


class AcademicJobStore(Protocol):
    """Durable job lifecycle required by application use cases."""

    def get_or_create(
        self,
        source_path: str,
        source_sha256: str,
        pipeline_signature: str,
        *,
        assigned: bool = False,
    ) -> AcademicJob: ...

    def get_job(self, job_id: str) -> AcademicJob | None: ...

    def list_jobs(
        self,
        statuses: Collection[JobStatus] | None = None,
        *,
        include_invalidated: bool = False,
    ) -> tuple[AcademicJob, ...]: ...

    def mark_running(self, job_id: str, phase: str) -> AcademicJob: ...

    def mark_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        phase: str | None = None,
        failed_phase: str | None = None,
        error: str | None = None,
    ) -> AcademicJob: ...

    def record_checkpoint(
        self, job_id: str, checkpoint: JobCheckpoint
    ) -> JobCheckpoint: ...

    def resume_checkpoints(self, job_id: str) -> tuple[JobCheckpoint, ...]: ...

    def retry(self, job_id: str) -> AcademicJob: ...

    def recover_interrupted(self) -> tuple[AcademicJob, ...]: ...

    def set_assignment(
        self, job_id: str, course: str, class_date: str
    ) -> AcademicJob: ...

    def staging_usage(self, job_id: str | None = None) -> int: ...

    def complete(self, job_id: str) -> int: ...

    def discard(self, job_id: str) -> int: ...


class JobNotFoundError(LookupError):
    """Raised when a requested durable job does not exist."""
