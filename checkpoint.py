"""Checkpointing utilities for per-chunk SQLite tracking and Spot-safe shutdown."""

import hashlib
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional, Sequence, Tuple

from config import Config as AppConfig

logger = logging.getLogger(__name__)


class ChunkSpec(NamedTuple):
    """Defines deterministic boundaries for a chunk."""

    index: int
    start_seconds: float
    end_seconds: float
    plan_hash: str


class CheckpointManager:
    """Manage per-file SQLite checkpoint state and chunk artifact writes."""

    def __init__(
        self,
        db_path: Path,
        source_uri: str,
        fingerprint: str,
        total_chunks: int,
        config: AppConfig,
        resume: bool = True,
        reset: bool = False,
        sync_s3_uri: Optional[str] = None,
        s3_client: Optional[Any] = None,
    ):
        self.db_path = db_path
        self.source_uri = source_uri
        self.fingerprint = fingerprint
        self.total_chunks = total_chunks
        self.config = config
        self.resume = resume and not reset
        self.sync_s3_uri = sync_s3_uri
        self.s3_client = s3_client
        self._conn: Optional[sqlite3.Connection] = None
        self.job_id = f"job_{datetime.utcnow().isoformat()}"
        self.file_id: Optional[str] = None

        if reset and self.db_path.exists():
            logger.info("Resetting checkpoint db %s", self.db_path)
            self.db_path.unlink()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._open_connection()
        self._ensure_schema()
        self._ensure_file_and_job()

    def _open_connection(self) -> None:
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, check_same_thread=False
        )
        assert self._conn is not None
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        self._conn.row_factory = sqlite3.Row
        logger.info("Opened checkpoint db %s", self.db_path)

    def close(self) -> None:
        if not self._conn:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.DatabaseError as exc:
            logger.warning("Failed to checkpoint WAL: %s", exc)
        self._conn.commit()
        self._conn.close()
        self._conn = None
        logger.info("Checkpoint db closed %s", self.db_path)
        if self.sync_s3_uri and self.s3_client:
            self._sync_db_to_s3()

    def _sync_db_to_s3(self) -> None:
        logger.info(
            "Syncing checkpoint db %s to %s (sync logic not implemented in v1)",
            self.db_path,
            self.sync_s3_uri,
        )

    def _ensure_schema(self) -> None:
        assert self._conn is not None
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                source_uri TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                total_chunks INTEGER NOT NULL,
                done_chunks INTEGER NOT NULL DEFAULT 0,
                final_output_uri TEXT,
                final_output_sha256 TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                plan_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                transcript_uri TEXT,
                transcript_sha256 TEXT,
                last_error TEXT,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);")
        self._conn.commit()

    def _ensure_file_and_job(self) -> None:
        assert self._conn is not None
        now = datetime.utcnow().isoformat()
        cur = self._conn.cursor()
        cur.execute(
            "SELECT file_id FROM files WHERE source_uri = ? AND fingerprint = ?",
            (self.source_uri, self.fingerprint),
        )
        row = cur.fetchone()
        if row and self.resume:
            self.file_id = row[0]
            logger.info("Reusing checkpoint file %s", self.file_id)

            # Reconcile any running chunks (mark as abandoned for retry)
            cur.execute(
                "UPDATE chunks SET status = 'retryable_failed', last_error = 'Chunk abandoned due to interruption', updated_at = ? WHERE file_id = ? AND status = 'running'",
                (now, self.file_id),
            )
            self._conn.commit()
            logger.debug(
                "Reconciled %d running chunks to retryable_failed", cur.rowcount
            )
            return

        self.file_id = f"file_{hashlib.sha256(self.source_uri.encode()).hexdigest()}"

        # Use INSERT OR UPDATE instead of INSERT OR REPLACE to avoid ON DELETE CASCADE issues
        cur.execute(
            "INSERT OR REPLACE INTO jobs(job_id, created_at, status, updated_at) VALUES(?, ?, ?, ?)",
            (self.job_id, now, "running", now),
        )

        # Check if file exists before inserting to avoid unnecessary deletes
        cur.execute("SELECT 1 FROM files WHERE file_id = ?", (self.file_id,))
        if cur.fetchone():
            cur.execute(
                "UPDATE files SET job_id = ?, source_uri = ?, fingerprint = ?, total_chunks = ?, updated_at = ? WHERE file_id = ?",
                (
                    self.job_id,
                    self.source_uri,
                    self.fingerprint,
                    self.total_chunks,
                    now,
                    self.file_id,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO files(file_id, job_id, source_uri, fingerprint, total_chunks, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    self.file_id,
                    self.job_id,
                    self.source_uri,
                    self.fingerprint,
                    self.total_chunks,
                    now,
                ),
            )

        self._conn.commit()
        logger.info("Initialized checkpoint file %s", self.file_id)

    def register_chunks(self, specs: Sequence[ChunkSpec]) -> None:
        assert self._conn is not None
        now = datetime.utcnow().isoformat()
        with self._conn:
            for spec in specs:
                chunk_id = f"chunk_{self.file_id}_{spec.index}"
                self._conn.execute(
                    "INSERT OR IGNORE INTO chunks(chunk_id, file_id, chunk_index, start_seconds, end_seconds, plan_hash, status, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        self.file_id,
                        spec.index,
                        spec.start_seconds,
                        spec.end_seconds,
                        spec.plan_hash,
                        "pending",
                        now,
                    ),
                )

    def claim_next_chunk(self) -> Optional[Tuple[int, float, float, str]]:
        if not self._conn:
            return None
        now = datetime.utcnow().isoformat()
        with self._transaction():
            row = self._conn.execute(
                "SELECT chunk_index, start_seconds, end_seconds, plan_hash, chunk_id FROM chunks WHERE file_id = ? AND status IN ('pending', 'retryable_failed', 'abandoned') ORDER BY chunk_index LIMIT 1",
                (self.file_id,),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE chunks SET status = ?, started_at = ?, updated_at = ? WHERE chunk_id = ?",
                ("running", now, now, row[4]),
            )
            return (row[0], row[1], row[2], row[3])

    def mark_chunk_done(
        self, chunk_index: int, transcript_uri: str, transcript_sha256: str
    ) -> None:
        if not self._conn:
            return
        now = datetime.utcnow().isoformat()
        chunk_id = f"chunk_{self.file_id}_{chunk_index}"
        with self._conn:
            self._conn.execute(
                "UPDATE chunks SET status = ?, transcript_uri = ?, transcript_sha256 = ?, completed_at = ?, updated_at = ? WHERE chunk_id = ?",
                ("done", transcript_uri, transcript_sha256, now, now, chunk_id),
            )
            self._conn.execute(
                "UPDATE files SET done_chunks = done_chunks + 1, updated_at = ? WHERE file_id = ?",
                (now, self.file_id),
            )

    def mark_chunk_failed(
        self, chunk_index: int, error: str, permanent: bool = False
    ) -> None:
        if not self._conn:
            return
        now = datetime.utcnow().isoformat()
        status = "permanent_failed" if permanent else "retryable_failed"
        chunk_id = f"chunk_{self.file_id}_{chunk_index}"
        with self._conn:
            self._conn.execute(
                "UPDATE chunks SET status = ?, last_error = ?, updated_at = ? WHERE chunk_id = ?",
                (status, error, now, chunk_id),
            )

    def persist_final_output(self, uri: str, sha256: str) -> None:
        if not self._conn:
            return
        now = datetime.utcnow().isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE files SET final_output_uri = ?, final_output_sha256 = ?, updated_at = ? WHERE file_id = ?",
                (uri, sha256, now, self.file_id),
            )

    @contextmanager
    def _transaction(self):
        assert self._conn is not None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def write_chunk_artifact(
        self, chunk_dir: Path, chunk_index: int, text: str
    ) -> Tuple[Path, str]:
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = chunk_dir / f"chunk_{chunk_index:04d}.txt"
        tmp_path = chunk_dir / f"chunk_{chunk_index:04d}.txt.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, chunk_path)
        dir_fd = os.open(chunk_dir, os.O_RDONLY)
        os.fsync(dir_fd)
        os.close(dir_fd)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return chunk_path, sha


class ShutdownCoordinator:
    """Coordinate graceful shutdown via signals and optional IMDS polling."""

    def __init__(self, poll_imds: bool = False, poll_interval: float = 10.0):
        self.shutdown_requested = False
        self.reason: Optional[str] = None
        self.poll_imds = poll_imds
        self.poll_interval = poll_interval
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._thread: Optional[threading.Thread] = None
        if poll_imds:
            self._thread = threading.Thread(target=self._poll_imds_loop, daemon=True)
            self._thread.start()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.shutdown_requested = True
        self.reason = f"signal_{signum}"
        logger.warning("Shutdown requested via %s", self.reason)

    def _poll_imds_loop(self) -> None:
        while not self.shutdown_requested:
            try:
                token = self._fetch_imds_token()
                if token and self._check_interruption(token):
                    self.shutdown_requested = True
                    self.reason = "spot_imds"
                    logger.warning("Spot interruption detected via IMDSv2")
                    break
            except Exception:
                logger.debug("IMDS polling failed")
            time.sleep(self.poll_interval)

    def _fetch_imds_token(self) -> Optional[str]:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.read().decode()

    def _check_interruption(self, token: str) -> bool:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/spot/instance-action",
            headers={"X-aws-ec2-metadata-token": token},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def should_stop(self) -> bool:
        return self.shutdown_requested
