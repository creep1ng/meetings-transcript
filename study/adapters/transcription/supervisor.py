"""Persistent same-runtime worker supervision with process-group cancellation."""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TextIO

from study.application.ports.jobs import JobStatus
from study.application.ports.transcriber import (
    PROTOCOL_VERSION,
    ArtifactReference,
    EventHandler,
    TranscriptionOutcome,
    TranscriptionRequest,
    WorkerEvent,
)

from .ipc import (
    ArtifactValidationError,
    HandshakeError,
    IPCProtocolError,
    PipelineSignatureError,
    WorkerDiedError,
    artifacts_from_response,
    decode_message,
    encode_message,
    event_from_message,
    request_message,
    validate_handshake,
)

logger = logging.getLogger(__name__)
_EOF = object()


class WorkerReportedError(RuntimeError):
    def __init__(self, message: str, *, phase: str | None = None) -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(slots=True)
class _WorkItem:
    request: TranscriptionRequest
    future: Future[TranscriptionOutcome]
    on_event: EventHandler | None
    cancel_requested: bool = False


class WorkerSupervisor:
    """Serializes jobs through one persistent, lazily started child process."""

    def __init__(
        self,
        pipeline_signature: str,
        command: tuple[str, ...] | list[str] | None = None,
        *,
        protocol_version: int = PROTOCOL_VERSION,
        handshake_timeout: float = 5.0,
        terminate_timeout: float = 1.0,
        stderr_handler: Callable[[str], None] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not pipeline_signature:
            raise ValueError("pipeline signature must not be empty")
        self.pipeline_signature = pipeline_signature
        self.command = tuple(command or (sys.executable, "-m", "study.worker"))
        self.protocol_version = protocol_version
        self.handshake_timeout = handshake_timeout
        self.terminate_timeout = terminate_timeout
        self._stderr_handler = stderr_handler
        self._environment = dict(environment or {})
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._stdout: queue.Queue[str | object] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[str, _WorkItem] = {}
        self._current: _WorkItem | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._dispatcher = threading.Thread(
            target=self._dispatch,
            name="study-worker-supervisor",
            daemon=True,
        )
        self._dispatcher.start()

    @property
    def pid(self) -> int | None:
        with self._lock:
            process = self._process
            return (
                process.pid if process is not None and process.poll() is None else None
            )

    @property
    def is_running(self) -> bool:
        return self.pid is not None

    def submit(
        self,
        request: TranscriptionRequest,
        on_event: EventHandler | None = None,
    ) -> Future[TranscriptionOutcome]:
        future: Future[TranscriptionOutcome] = Future()
        if request.pipeline_signature != self.pipeline_signature:
            future.set_exception(
                HandshakeError("request pipeline signature does not match supervisor")
            )
            return future
        item = _WorkItem(request=request, future=future, on_event=on_event)
        with self._lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if request.job_id in self._pending:
                raise ValueError(f"job {request.job_id} is already queued")
            self._pending[request.job_id] = item
            self._queue.put(item)
        return future

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            item = self._pending.get(job_id)
            if item is None or item.future.done():
                return False
            item.cancel_requested = True
            is_current = self._current is item
        if is_current:
            self._terminate_worker()
        item.future.cancel()
        return True

    def _dispatch(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if item.future.cancelled():
                with self._lock:
                    self._pending.pop(item.request.job_id, None)
                continue
            with self._lock:
                self._current = item
            try:
                outcome = self._run_item(item)
            except CancelledError:
                item.future.cancel()
            except BaseException as exc:
                if item.cancel_requested:
                    item.future.cancel()
                elif not isinstance(exc, WorkerReportedError):
                    self._terminate_worker()
                if not item.cancel_requested and not item.future.done():
                    item.future.set_exception(exc)
            else:
                if not item.future.done():
                    item.future.set_result(outcome)
            finally:
                with self._lock:
                    if self._current is item:
                        self._current = None
                    self._pending.pop(item.request.job_id, None)

    def _run_item(self, item: _WorkItem) -> TranscriptionOutcome:
        process = self._ensure_worker()
        self._send(
            process,
            request_message(item.request, protocol_version=self.protocol_version),
        )
        completed_phases: list[str] = []
        failed_phases: list[str] = []
        reported_artifacts: set[tuple[str, str]] = set()
        while True:
            if item.cancel_requested or item.future.cancelled():
                raise CancelledError()
            message = self._read_message(timeout=None)
            message_type = message["type"]
            job_id = message.get("id")
            if job_id != item.request.job_id:
                raise IPCProtocolError(
                    f"worker event for {job_id!r} while running {item.request.job_id!r}"
                )
            if message_type == "event":
                event = event_from_message(message)
                if event.message == "completed" and event.phase:
                    completed_phases.append(event.phase)
                elif event.message == "failed" and event.phase:
                    failed_phases.append(event.phase)
                if item.on_event is not None:
                    item.on_event(event)
                    phase_artifact = self._phase_artifact(event.phase, item)
                    if phase_artifact is not None:
                        reported_artifacts.add(
                            (phase_artifact.path, phase_artifact.sha256)
                        )
                        item.on_event(
                            WorkerEvent(
                                kind="checkpoint",
                                job_id=item.request.job_id,
                                phase=event.phase,
                                artifact=phase_artifact,
                            )
                        )
                continue
            if message_type != "response":
                raise IPCProtocolError(
                    f"unexpected worker message during job: {message_type!r}"
                )
            for phase, artifact in artifacts_from_response(message):
                self._validate_artifact(artifact.path, artifact.sha256, item)
                if (
                    item.on_event is not None
                    and (artifact.path, artifact.sha256) not in reported_artifacts
                ):
                    item.on_event(
                        WorkerEvent(
                            kind="checkpoint",
                            job_id=item.request.job_id,
                            phase=phase,
                            artifact=artifact,
                        )
                    )
            if not message["ok"]:
                error = message["error"]
                raise WorkerReportedError(
                    error["message"],
                    phase=failed_phases[0]
                    if failed_phases
                    else item.request.start_phase,
                )
            result = message["result"]
            response_signature = result.get("pipeline_signature")
            if (
                response_signature is not None
                and response_signature != self.pipeline_signature
            ):
                raise PipelineSignatureError(
                    "worker response pipeline signature does not match"
                )
            wire_status = result.get("status")
            if wire_status == "complete":
                status = JobStatus.COMPLETED
            elif wire_status == "partial":
                status = JobStatus.PARTIAL
            else:
                raise IPCProtocolError(
                    f"worker result status is invalid: {wire_status!r}"
                )
            return TranscriptionOutcome(
                job_id=item.request.job_id,
                status=status,
                completed_phases=tuple(
                    result.get("completed_phases") or dict.fromkeys(completed_phases)
                ),
                failed_phase=(result.get("failed_phases") or failed_phases or [None])[
                    0
                ],
                error=result.get("error"),
                assigned=result.get("assigned"),
            )

    def _ensure_worker(self) -> subprocess.Popen[str]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                return process
            self._process = None
            stdout_queue: queue.Queue[str | object] = queue.Queue()
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                start_new_session=os.name == "posix",
                env={**os.environ, **self._environment},
            )
            self._process = process
            self._stdout = stdout_queue
            assert process.stdout is not None
            assert process.stderr is not None
            threading.Thread(
                target=self._stdout_pump,
                args=(process.stdout, stdout_queue),
                name=f"study-worker-stdout-{process.pid}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._stderr_pump,
                args=(process.stderr,),
                name=f"study-worker-stderr-{process.pid}",
                daemon=True,
            ).start()

        try:
            response = self._read_message(timeout=self.handshake_timeout)
            validate_handshake(
                response,
                self.pipeline_signature,
                protocol_version=self.protocol_version,
            )
        except BaseException:
            self._terminate_worker(process)
            raise
        return process

    @staticmethod
    def _stdout_pump(stream: TextIO, output: queue.Queue[str | object]) -> None:
        try:
            while line := stream.readline(64 * 1024 + 2):
                output.put(line)
        except (OSError, UnicodeError):
            pass
        finally:
            output.put(_EOF)

    def _stderr_pump(self, stream: TextIO) -> None:
        try:
            while line := stream.readline():
                text = line.rstrip("\r\n")
                if self._stderr_handler is not None:
                    self._stderr_handler(text)
                else:
                    logger.info("study worker: %s", text)
        except (OSError, UnicodeError):
            return

    def _read_message(self, *, timeout: float | None) -> dict[str, object]:
        output = self._stdout
        if output is None:
            raise WorkerDiedError("worker stdout is unavailable")
        try:
            line = output.get(timeout=timeout)
        except queue.Empty as exc:
            raise HandshakeError("worker handshake timed out") from exc
        if line is _EOF:
            with self._lock:
                process = self._process
            return_code = process.poll() if process is not None else None
            raise WorkerDiedError(
                f"worker closed stdout before completion (exit {return_code})"
            )
        assert isinstance(line, str)
        return decode_message(line)

    @staticmethod
    def _send(process: subprocess.Popen[str], message: dict[str, object]) -> None:
        if process.poll() is not None or process.stdin is None:
            raise WorkerDiedError(f"worker exited with code {process.poll()}")
        try:
            process.stdin.write(encode_message(message))
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerDiedError("worker closed stdin") from exc

    @staticmethod
    def _phase_artifact(
        phase: str | None,
        item: _WorkItem,
    ) -> ArtifactReference | None:
        if not phase:
            return None
        staging = Path(item.request.staging_path).resolve()
        try:
            artifact = (staging / f"{phase}.json").resolve(strict=True)
            artifact.relative_to(staging)
        except (OSError, ValueError):
            return None
        if not artifact.is_file():
            return None
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return ArtifactReference(path=str(artifact), sha256=digest.hexdigest())

    @staticmethod
    def _validate_artifact(
        artifact_path: str,
        expected_sha256: str,
        item: _WorkItem,
    ) -> None:
        staging = Path(item.request.staging_path).resolve()
        try:
            artifact = Path(artifact_path).resolve(strict=True)
            artifact.relative_to(staging)
        except (OSError, ValueError) as exc:
            raise ArtifactValidationError(
                "worker artifact is missing or outside job staging"
            ) from exc
        if not artifact.is_file():
            raise ArtifactValidationError("worker artifact is not a regular file")
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ArtifactValidationError("worker artifact SHA-256 does not match")

    def _terminate_worker(self, expected: subprocess.Popen[str] | None = None) -> None:
        with self._lock:
            process = self._process
            if process is None or (expected is not None and process is not expected):
                return
            self._process = None
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=self.terminate_timeout)
            except subprocess.TimeoutExpired:
                pass
            try:
                # The group may outlive its leader when a descendant ignores TERM.
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=self.terminate_timeout)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "study worker %s did not exit after SIGKILL", process.pid
                    )
            return
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=self.terminate_timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=self.terminate_timeout)
            except subprocess.TimeoutExpired:
                logger.error("study worker %s did not exit after SIGKILL", process.pid)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending.values())
            for item in pending:
                item.cancel_requested = True
        self._terminate_worker()
        for item in pending:
            item.future.cancel()
        self._queue.put(None)
        self._dispatcher.join(timeout=self.terminate_timeout * 2 + 1)

    def __enter__(self) -> WorkerSupervisor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
