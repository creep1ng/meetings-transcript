"""Dispatch media transcription while persisting every lifecycle transition."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from typing import Mapping

from ..ports.jobs import AcademicJob, AcademicJobStore, JobCheckpoint, JobStatus
from ..ports.transcriber import (
    Transcriber,
    TranscriptionOutcome,
    TranscriptionRequest,
    WorkerEvent,
)


@dataclass(frozen=True, slots=True)
class TranscribeMediaCommand:
    source_path: str
    source_sha256: str
    pipeline_signature: str
    assigned: bool = False
    requested_language: str = "es"
    media_kind: str = "audio"
    assignment: Mapping[str, str] | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None


@dataclass(frozen=True, slots=True)
class SubmittedTranscription:
    job: AcademicJob
    future: Future[TranscriptionOutcome]


class TranscribeMedia:
    def __init__(self, jobs: AcademicJobStore, transcriber: Transcriber) -> None:
        self._jobs = jobs
        self._transcriber = transcriber

    def execute(self, command: TranscribeMediaCommand) -> SubmittedTranscription:
        job = self._jobs.get_or_create(
            command.source_path,
            command.source_sha256,
            command.pipeline_signature,
            assigned=command.assigned,
        )
        checkpoints = self._jobs.resume_checkpoints(job.id)
        job = self._jobs.retry(job.id) if job.status is not JobStatus.PENDING else job
        start_phase = job.retry_phase
        job = self._jobs.mark_running(job.id, start_phase)
        request = TranscriptionRequest(
            job_id=job.id,
            source_path=job.source_path,
            source_sha256=job.source_sha256,
            pipeline_signature=job.pipeline_signature,
            staging_path=job.staging_path,
            start_phase=start_phase,
            checkpoints=checkpoints,
            options={
                "language": command.requested_language,
                "media_kind": command.media_kind,
                "assignment": command.assignment or {},
                "min_speakers": command.min_speakers,
                "max_speakers": command.max_speakers,
            },
        )
        try:
            future = self._transcriber.submit(request, self._on_event)
        except Exception as exc:
            self._jobs.mark_status(
                job.id,
                JobStatus.FAILED,
                failed_phase=start_phase,
                error=str(exc),
            )
            raise
        future.add_done_callback(
            lambda completed, job_id=job.id, assigned=command.assigned: self._on_done(
                job_id, assigned, completed
            )
        )
        return SubmittedTranscription(job=job, future=future)

    def cancel(self, job_id: str) -> bool:
        cancelled = self._transcriber.cancel(job_id)
        if cancelled:
            self._jobs.mark_status(job_id, JobStatus.CANCELLED)
        return cancelled

    def _on_event(self, event: WorkerEvent) -> None:
        if event.kind == "progress" and event.phase:
            self._jobs.mark_running(event.job_id, event.phase)
        elif event.kind == "checkpoint" and event.phase and event.artifact:
            self._jobs.record_checkpoint(
                event.job_id,
                JobCheckpoint(
                    phase=event.phase,
                    artifact_path=event.artifact.path,
                    artifact_sha256=event.artifact.sha256,
                ),
            )

    def _on_done(
        self,
        job_id: str,
        assigned: bool,
        future: Future[TranscriptionOutcome],
    ) -> None:
        try:
            outcome = future.result()
        except CancelledError:
            self._jobs.mark_status(job_id, JobStatus.CANCELLED)
        except Exception as exc:
            job = self._jobs.get_job(job_id)
            phase = job.current_phase if job else None
            self._jobs.mark_status(
                job_id,
                JobStatus.FAILED,
                failed_phase=phase,
                error=str(exc),
            )
        else:
            if outcome.status is JobStatus.PARTIAL:
                self._jobs.mark_status(
                    job_id,
                    JobStatus.PARTIAL,
                    failed_phase=outcome.failed_phase,
                    error=outcome.error,
                )
            else:
                current = self._jobs.get_job(job_id)
                result_assigned = bool(current and current.assigned) or (
                    outcome.assigned if outcome.assigned is not None else assigned
                )
                status = (
                    JobStatus.READY_TO_COMMIT
                    if result_assigned
                    else JobStatus.UNASSIGNED
                )
                self._jobs.mark_status(job_id, status)
