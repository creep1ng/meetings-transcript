"""Boundary types for a supervised transcription worker."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .jobs import JobCheckpoint, JobStatus

PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """A worker artifact reference; content remains off the IPC channel."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    job_id: str
    source_path: str
    source_sha256: str
    pipeline_signature: str
    staging_path: str
    start_phase: str = "asr"
    checkpoints: tuple[JobCheckpoint, ...] = ()
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    kind: str
    job_id: str
    phase: str | None = None
    artifact: ArtifactReference | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionOutcome:
    job_id: str
    status: JobStatus
    completed_phases: tuple[str, ...] = ()
    failed_phase: str | None = None
    error: str | None = None
    assigned: bool | None = None


EventHandler = Callable[[WorkerEvent], None]


class Transcriber(Protocol):
    """Queue work without exposing process APIs to the application."""

    def submit(
        self,
        request: TranscriptionRequest,
        on_event: EventHandler | None = None,
    ) -> Future[TranscriptionOutcome]: ...

    def cancel(self, job_id: str) -> bool: ...

    def close(self) -> None: ...
