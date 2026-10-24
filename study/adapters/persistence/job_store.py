"""SQLite-backed academic jobs and phase checkpoints."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from study.application.ports.jobs import (
    AcademicJob,
    JobCheckpoint,
    JobNotFoundError,
    JobStatus,
)

_SCHEMA_VERSION = 2
_SHA256_LENGTH = 64
_PIPELINE_PHASES = frozenset({"asr", "alignment", "diarization"})


class ArtifactValidationError(ValueError):
    """Raised when a checkpoint points outside staging or has changed content."""


class InvalidatedJobError(RuntimeError):
    """Raised when stale work is retried after its source or pipeline changed."""


def default_state_root() -> Path:
    configured = os.environ.get("ACADEMIC_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local/state"
    return base / "meetings-transcript/academic"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_sha256(value: str, *, label: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a hexadecimal SHA-256 digest")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AcademicJobStore:
    """Owns recoverable state outside the synchronized academic library."""

    def __init__(
        self,
        state_root: str | os.PathLike[str] | None = None,
        *,
        library_root: str | os.PathLike[str] | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.state_root = (
            Path(state_root or default_state_root()).expanduser().resolve()
        )
        library_root = library_root or os.environ.get("ACADEMIC_LIBRARY_ROOT")
        if library_root is not None:
            library = Path(library_root).expanduser().resolve()
            if self.state_root == library or self.state_root.is_relative_to(library):
                raise ValueError("academic state must be stored outside the library")

        self.staging_root = self.state_root / "staging"
        self.db_path = self.state_root / "jobs.sqlite3"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        journal_mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            self._conn.close()
            raise sqlite3.OperationalError("academic job database could not enable WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._ensure_open()
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 1:
                self._migrate_v1_to_v2()
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_size INTEGER,
                    source_mtime_ns INTEGER,
                    pipeline_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    staging_path TEXT NOT NULL,
                    current_phase TEXT,
                    failed_phase TEXT,
                    error TEXT,
                    assigned INTEGER NOT NULL DEFAULT 0,
                    course TEXT,
                    class_date TEXT,
                    invalidated INTEGER NOT NULL DEFAULT 0,
                    invalidation_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS jobs_source_path_idx
                    ON jobs(source_path);
                CREATE INDEX IF NOT EXISTS jobs_status_idx
                    ON jobs(status, updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_identity_idx
                    ON jobs(source_path, source_sha256, pipeline_signature);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, phase),
                    UNIQUE(job_id, sequence)
                );
                """
            )
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _migrate_v1_to_v2(self) -> None:
        """Rebuild v1 so equal bytes at different paths remain distinct jobs."""

        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE checkpoints RENAME TO checkpoints_v1;
                ALTER TABLE jobs RENAME TO jobs_v1;
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_size INTEGER,
                    source_mtime_ns INTEGER,
                    pipeline_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    staging_path TEXT NOT NULL,
                    current_phase TEXT,
                    failed_phase TEXT,
                    error TEXT,
                    assigned INTEGER NOT NULL DEFAULT 0,
                    course TEXT,
                    class_date TEXT,
                    invalidated INTEGER NOT NULL DEFAULT 0,
                    invalidation_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO jobs (
                    id, source_path, source_sha256, source_size, source_mtime_ns,
                    pipeline_signature, status, staging_path, current_phase,
                    failed_phase, error, assigned, invalidated,
                    invalidation_reason, created_at, updated_at
                ) SELECT
                    id, source_path, source_sha256, source_size, source_mtime_ns,
                    pipeline_signature, status, staging_path, current_phase,
                    failed_phase, error, assigned, invalidated,
                    invalidation_reason, created_at, updated_at
                FROM jobs_v1;
                CREATE TABLE checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, phase),
                    UNIQUE(job_id, sequence)
                );
                INSERT INTO checkpoints SELECT * FROM checkpoints_v1;
                DROP TABLE checkpoints_v1;
                DROP TABLE jobs_v1;
                COMMIT;
                """
            )
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("academic job store is closed")

    @staticmethod
    def job_id_for(
        source_sha256: str, pipeline_signature: str, source_path: str = ""
    ) -> str:
        source_sha256 = _validate_sha256(source_sha256, label="source hash")
        if not pipeline_signature:
            raise ValueError("pipeline signature must not be empty")
        identity = f"{source_path}\0{source_sha256}\0{pipeline_signature}".encode(
            "utf-8"
        )
        return hashlib.sha256(identity).hexdigest()

    def get_or_create(
        self,
        source_path: str,
        source_sha256: str,
        pipeline_signature: str,
        *,
        assigned: bool = False,
    ) -> AcademicJob:
        source_sha256 = _validate_sha256(source_sha256, label="source hash")
        if not source_path:
            raise ValueError("source path must not be empty")
        job_id = self.job_id_for(source_sha256, pipeline_signature, source_path)
        source = Path(source_path)
        try:
            source_stat = source.stat()
            source_size: int | None = source_stat.st_size
            source_mtime_ns: int | None = source_stat.st_mtime_ns
        except OSError:
            source_size = None
            source_mtime_ns = None
        now = _utc_now()
        staging_path = self.staging_root / job_id

        with self._transaction():
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?, invalidated = 1,
                       invalidation_reason = ?, updated_at = ?
                 WHERE source_path = ? AND id != ? AND invalidated = 0
                """,
                (
                    JobStatus.RECOVERABLE.value,
                    "source hash or pipeline signature changed",
                    now,
                    source_path,
                    job_id,
                ),
            )
            existing = self._conn.execute(
                "SELECT invalidated FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO jobs (
                        id, source_path, source_sha256, source_size,
                        source_mtime_ns, pipeline_signature, status,
                        staging_path, assigned, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        source_path,
                        source_sha256,
                        source_size,
                        source_mtime_ns,
                        pipeline_signature,
                        JobStatus.PENDING.value,
                        str(staging_path),
                        int(assigned),
                        now,
                        now,
                    ),
                )
            elif existing["invalidated"]:
                self._conn.execute(
                    """
                    UPDATE jobs
                       SET source_path = ?, source_size = ?, source_mtime_ns = ?,
                           status = ?, assigned = ?, invalidated = 0,
                           invalidation_reason = NULL, error = NULL, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        source_path,
                        source_size,
                        source_mtime_ns,
                        JobStatus.PENDING.value,
                        int(assigned),
                        now,
                        job_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE jobs
                       SET source_path = ?, source_size = ?, source_mtime_ns = ?,
                           assigned = MAX(assigned, ?), updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        source_path,
                        source_size,
                        source_mtime_ns,
                        int(assigned),
                        now,
                        job_id,
                    ),
                )

        staging_path.mkdir(parents=True, exist_ok=True)
        job = self.get_job(job_id)
        assert job is not None
        return job

    create_job = get_or_create

    def get_job(self, job_id: str) -> AcademicJob | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._job_from_row(row) if row is not None else None

    get = get_job

    def _require_job(self, job_id: str) -> AcademicJob:
        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def _job_from_row(self, row: sqlite3.Row) -> AcademicJob:
        checkpoint_rows = self._conn.execute(
            """
            SELECT phase, artifact_path, artifact_sha256, sequence, created_at
              FROM checkpoints WHERE job_id = ? ORDER BY sequence
            """,
            (row["id"],),
        ).fetchall()
        checkpoints = tuple(
            JobCheckpoint(
                phase=item["phase"],
                artifact_path=item["artifact_path"],
                artifact_sha256=item["artifact_sha256"],
                sequence=item["sequence"],
                created_at=item["created_at"],
            )
            for item in checkpoint_rows
        )
        return AcademicJob(
            id=row["id"],
            source_path=row["source_path"],
            source_sha256=row["source_sha256"],
            pipeline_signature=row["pipeline_signature"],
            status=JobStatus(row["status"]),
            staging_path=row["staging_path"],
            current_phase=row["current_phase"],
            failed_phase=row["failed_phase"],
            error=row["error"],
            assigned=bool(row["assigned"]),
            course=row["course"],
            class_date=row["class_date"],
            invalidated=bool(row["invalidated"]),
            invalidation_reason=row["invalidation_reason"],
            checkpoints=checkpoints,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_jobs(
        self,
        statuses: Collection[JobStatus] | None = None,
        *,
        include_invalidated: bool = False,
    ) -> tuple[AcademicJob, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if statuses is not None:
            values = sorted({JobStatus(status).value for status in statuses})
            if not values:
                return ()
            clauses.append(f"status IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
        if not include_invalidated:
            clauses.append("invalidated = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at, id", parameters
            ).fetchall()
            return tuple(self._job_from_row(row) for row in rows)

    def mark_running(self, job_id: str, phase: str) -> AcademicJob:
        if not phase:
            raise ValueError("running phase must not be empty")
        now = _utc_now()
        with self._transaction():
            updated = self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?, current_phase = ?, failed_phase = NULL,
                       error = NULL, updated_at = ?
                 WHERE id = ? AND invalidated = 0
                """,
                (JobStatus.RUNNING.value, phase, now, job_id),
            ).rowcount
            if not updated:
                self._raise_missing_or_invalidated(job_id)
        return self._require_job(job_id)

    def mark_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        phase: str | None = None,
        failed_phase: str | None = None,
        error: str | None = None,
    ) -> AcademicJob:
        status = JobStatus(status)
        if status in {JobStatus.FAILED, JobStatus.PARTIAL}:
            failed_phase = failed_phase or phase
        now = _utc_now()
        with self._transaction():
            updated = self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?, current_phase = COALESCE(?, current_phase),
                       failed_phase = ?, error = ?, updated_at = ?
                 WHERE id = ? AND invalidated = 0
                """,
                (
                    status.value,
                    phase,
                    failed_phase,
                    error,
                    now,
                    job_id,
                ),
            ).rowcount
            if not updated:
                self._raise_missing_or_invalidated(job_id)
        return self._require_job(job_id)

    def _raise_missing_or_invalidated(self, job_id: str) -> None:
        row = self._conn.execute(
            "SELECT invalidated FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        raise InvalidatedJobError(f"job {job_id} has been invalidated")

    def record_checkpoint(
        self, job_id: str, checkpoint: JobCheckpoint
    ) -> JobCheckpoint:
        job = self._require_job(job_id)
        if job.invalidated:
            raise InvalidatedJobError(f"job {job_id} has been invalidated")
        if not checkpoint.phase:
            raise ValueError("checkpoint phase must not be empty")
        artifact_sha256 = _validate_sha256(
            checkpoint.artifact_sha256, label="artifact hash"
        )
        artifact_path = self._validated_artifact_path(
            job, checkpoint.artifact_path, artifact_sha256
        )
        now = checkpoint.created_at or _utc_now()
        with self._transaction():
            existing = self._conn.execute(
                "SELECT sequence FROM checkpoints WHERE job_id = ? AND phase = ?",
                (job_id, checkpoint.phase),
            ).fetchone()
            if existing is not None:
                sequence = existing["sequence"]
            elif checkpoint.sequence > 0:
                sequence = checkpoint.sequence
            else:
                sequence = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM checkpoints WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
            self._conn.execute(
                """
                INSERT INTO checkpoints (
                    job_id, phase, artifact_path, artifact_sha256, sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, phase) DO UPDATE SET
                    artifact_path = excluded.artifact_path,
                    artifact_sha256 = excluded.artifact_sha256,
                    created_at = excluded.created_at
                """,
                (
                    job_id,
                    checkpoint.phase,
                    str(artifact_path),
                    artifact_sha256,
                    sequence,
                    now,
                ),
            )
            if checkpoint.phase in _PIPELINE_PHASES:
                self._conn.execute(
                    "UPDATE jobs SET current_phase = ?, updated_at = ? WHERE id = ?",
                    (checkpoint.phase, _utc_now(), job_id),
                )
        return JobCheckpoint(
            phase=checkpoint.phase,
            artifact_path=str(artifact_path),
            artifact_sha256=artifact_sha256,
            sequence=sequence,
            created_at=now,
        )

    checkpoint = record_checkpoint

    def _validated_artifact_path(
        self, job: AcademicJob, artifact_path: str, expected_sha256: str
    ) -> Path:
        staging = Path(job.staging_path).resolve()
        candidate = Path(artifact_path).resolve(strict=True)
        try:
            candidate.relative_to(staging)
        except ValueError as exc:
            raise ArtifactValidationError("artifact is outside job staging") from exc
        if not candidate.is_file():
            raise ArtifactValidationError("artifact is not a regular file")
        actual_sha256 = _sha256_file(candidate)
        if actual_sha256 != expected_sha256:
            raise ArtifactValidationError(
                f"artifact hash mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        return candidate

    def resume_checkpoints(self, job_id: str) -> tuple[JobCheckpoint, ...]:
        job = self._require_job(job_id)
        if job.invalidated:
            return ()
        if not self._source_is_unchanged(job):
            self._invalidate(job_id, "source stat or SHA-256 changed")
            return ()

        valid: list[JobCheckpoint] = []
        for checkpoint in job.checkpoints:
            if checkpoint.phase not in _PIPELINE_PHASES:
                continue
            try:
                self._validated_artifact_path(
                    job, checkpoint.artifact_path, checkpoint.artifact_sha256
                )
            except (ArtifactValidationError, OSError):
                with self._transaction():
                    self._conn.execute(
                        "DELETE FROM checkpoints WHERE job_id = ? AND sequence >= ?",
                        (job_id, checkpoint.sequence),
                    )
                    self._conn.execute(
                        """
                        UPDATE jobs SET status = ?, current_phase = ?, failed_phase = ?,
                                        error = ?, updated_at = ? WHERE id = ?
                        """,
                        (
                            JobStatus.RECOVERABLE.value,
                            checkpoint.phase,
                            checkpoint.phase,
                            "checkpoint artifact is missing or invalid",
                            _utc_now(),
                            job_id,
                        ),
                    )
                break
            valid.append(checkpoint)
        return tuple(valid)

    def _source_is_unchanged(self, job: AcademicJob) -> bool:
        source = Path(job.source_path)
        try:
            if not source.is_file():
                return False
            before = source.stat()
            source_sha256 = _sha256_file(source)
            after = source.stat()
            stable_stat = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            return stable_stat and source_sha256 == job.source_sha256
        except OSError:
            return False

    def _invalidate(self, job_id: str, reason: str) -> None:
        with self._transaction():
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, invalidated = 1,
                                invalidation_reason = ?, updated_at = ?
                 WHERE id = ?
                """,
                (JobStatus.RECOVERABLE.value, reason, _utc_now(), job_id),
            )

    def retry(self, job_id: str) -> AcademicJob:
        job = self._require_job(job_id)
        if job.invalidated:
            raise InvalidatedJobError(f"job {job_id} has been invalidated")
        self.resume_checkpoints(job_id)
        job = self._require_job(job_id)
        if job.invalidated:
            raise InvalidatedJobError(f"job {job_id} source no longer matches")
        retry_phase = job.retry_phase
        with self._transaction():
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, current_phase = ?, failed_phase = NULL,
                                error = NULL, updated_at = ? WHERE id = ?
                """,
                (JobStatus.PENDING.value, retry_phase, _utc_now(), job_id),
            )
        return self._require_job(job_id)

    def recover_interrupted(self) -> tuple[AcademicJob, ...]:
        now = _utc_now()
        with self._transaction():
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status = ? AND invalidated = 0",
                (JobStatus.RUNNING.value,),
            ).fetchall()
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, error = ?, updated_at = ?
                 WHERE status = ? AND invalidated = 0
                """,
                (
                    JobStatus.RECOVERABLE.value,
                    "worker stopped before the phase completed",
                    now,
                    JobStatus.RUNNING.value,
                ),
            )
        return tuple(self._require_job(row["id"]) for row in rows)

    def set_assigned(self, job_id: str, assigned: bool = True) -> AcademicJob:
        with self._transaction():
            updated = self._conn.execute(
                "UPDATE jobs SET assigned = ?, updated_at = ? WHERE id = ?",
                (int(assigned), _utc_now(), job_id),
            ).rowcount
            if not updated:
                raise JobNotFoundError(job_id)
        return self._require_job(job_id)

    def set_assignment(self, job_id: str, course: str, class_date: str) -> AcademicJob:
        if not course.strip():
            raise ValueError("course must not be empty")
        try:
            normalized_date = datetime.fromisoformat(class_date).date().isoformat()
        except ValueError as error:
            raise ValueError("class_date must be an ISO date") from error
        if normalized_date != class_date:
            raise ValueError("class_date must be an ISO date")
        with self._transaction():
            updated = self._conn.execute(
                """
                UPDATE jobs
                   SET assigned = 1, course = ?, class_date = ?,
                       status = CASE WHEN status = ? THEN ? ELSE status END,
                       updated_at = ?
                 WHERE id = ? AND invalidated = 0
                """,
                (
                    course.strip(),
                    class_date,
                    JobStatus.UNASSIGNED.value,
                    JobStatus.READY_TO_COMMIT.value,
                    _utc_now(),
                    job_id,
                ),
            ).rowcount
            if not updated:
                self._raise_missing_or_invalidated(job_id)
        return self._require_job(job_id)

    def staging_usage(self, job_id: str | None = None) -> int:
        root = (
            self.staging_root
            if job_id is None
            else Path(self._require_job(job_id).staging_path)
        )
        try:
            root = root.resolve()
            root.relative_to(self.staging_root.resolve())
        except (OSError, ValueError):
            return 0
        if not root.exists():
            return 0
        total = 0
        for directory, _, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                try:
                    total += (Path(directory) / filename).lstat().st_size
                except OSError:
                    continue
        return total

    def discard(self, job_id: str) -> int:
        job = self._require_job(job_id)
        staging = Path(job.staging_path).resolve()
        try:
            staging.relative_to(self.staging_root.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "refusing to discard staging outside the state root"
            ) from exc
        reclaimed = self.staging_usage(job_id)
        if staging.exists():
            shutil.rmtree(staging)
        with self._transaction():
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return reclaimed

    def complete(self, job_id: str) -> int:
        """Finalize a published job and remove now-unneeded staging artifacts."""

        job = self._require_job(job_id)
        staging = Path(job.staging_path).resolve()
        try:
            staging.relative_to(self.staging_root.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "refusing to clean staging outside the state root"
            ) from exc
        reclaimed = self.staging_usage(job_id)
        if staging.exists():
            shutil.rmtree(staging)
        with self._transaction():
            self._conn.execute("DELETE FROM checkpoints WHERE job_id = ?", (job_id,))
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, current_phase = NULL,
                                failed_phase = NULL, error = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (JobStatus.COMPLETED.value, _utc_now(), job_id),
            )
        return reclaimed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __enter__(self) -> AcademicJobStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
