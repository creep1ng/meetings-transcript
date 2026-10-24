"""List, retry, and discard retained academic work."""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.jobs import RECOVERABLE_STATUSES, AcademicJob, AcademicJobStore


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    job: AcademicJob
    staging_bytes: int
    retry_phase: str


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    jobs: tuple[RecoveryEntry, ...]
    total_staging_bytes: int


class RecoverJobs:
    def __init__(self, jobs: AcademicJobStore) -> None:
        self._jobs = jobs

    def execute(self) -> RecoverySnapshot:
        self._jobs.recover_interrupted()
        recoverable = self._jobs.list_jobs(
            RECOVERABLE_STATUSES, include_invalidated=True
        )
        entries = tuple(
            RecoveryEntry(
                job=job,
                staging_bytes=self._jobs.staging_usage(job.id),
                retry_phase=job.retry_phase,
            )
            for job in recoverable
        )
        return RecoverySnapshot(
            jobs=entries,
            total_staging_bytes=self._jobs.staging_usage(),
        )

    def retry(self, job_id: str) -> AcademicJob:
        return self._jobs.retry(job_id)

    def discard(self, job_id: str) -> int:
        return self._jobs.discard(job_id)
