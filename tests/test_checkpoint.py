"""Tests for the per-chunk checkpoint manager."""

import sqlite3
from pathlib import Path
from typing import List

import pytest

from checkpoint import CheckpointManager, ChunkSpec
from config import Config


def make_specs(count: int) -> List[ChunkSpec]:
    """Return a list of deterministically hashed chunk specs."""

    return [
        ChunkSpec(
            index=i,
            start_seconds=float(i),
            end_seconds=float(i + 1),
            plan_hash=f"plan-{i}",
        )
        for i in range(count)
    ]


def done_chunks(manager: CheckpointManager) -> int:
    """Query the files table for the done chunk count."""

    assert manager._conn is not None
    row = manager._conn.execute(
        "SELECT done_chunks FROM files WHERE file_id = ?", (manager.file_id,)
    ).fetchone()
    return 0 if not row else row[0]


def build_manager(
    temp_dir: Path,
    config: Config,
    chunks: int,
    *,
    resume: bool = True,
    reset: bool = False,
    db_name: str = "checkpoint.sqlite",
) -> CheckpointManager:
    """Helper to instantiate a checkpoint manager with deterministic params."""

    return CheckpointManager(
        db_path=temp_dir / db_name,
        source_uri="fake://meeting",
        fingerprint="fake-fingerprint",
        total_chunks=chunks,
        config=config,
        resume=resume,
        reset=reset,
    )


def process_chunks(manager: CheckpointManager, *, limit: int) -> List[int]:
    """Claim and complete at most `limit` chunks for the manager."""

    completed = []
    while len(completed) < limit:
        spec = manager.claim_next_chunk()
        if spec is None:
            break
        completed.append(spec[0])
        manager.mark_chunk_done(spec[0], f"uri-{spec[0]}", f"sha-{spec[0]}")
    return completed


def test_resume_skips_done_chunks(temp_dir: Path):
    config = Config()
    specs = make_specs(5)
    manager1 = build_manager(temp_dir, config, len(specs), db_name="resume.sqlite")
    manager1.register_chunks(specs)
    assert process_chunks(manager1, limit=2) == [0, 1]
    assert done_chunks(manager1) == 2
    first_file_id = manager1.file_id
    manager1.close()

    manager2 = build_manager(temp_dir, config, len(specs), db_name="resume.sqlite")
    manager2.register_chunks(specs)
    assert manager2.file_id == first_file_id

    remaining = []
    while True:
        spec = manager2.claim_next_chunk()
        if spec is None:
            break
        remaining.append(spec[0])
        manager2.mark_chunk_done(spec[0], f"uri-{spec[0]}", f"sha-{spec[0]}")
    assert remaining == [2, 3, 4]
    assert done_chunks(manager2) == len(specs)
    manager2.close()


def test_reset_checkpoint_reprocesses_existing_data(temp_dir: Path):
    config = Config()
    specs = make_specs(3)
    manager = build_manager(temp_dir, config, len(specs), db_name="reset.sqlite")
    manager.register_chunks(specs)
    spec = manager.claim_next_chunk()
    assert spec is not None
    manager.mark_chunk_done(spec[0], "uri", "sha")
    assert done_chunks(manager) == 1
    manager.close()

    reset_manager = build_manager(
        temp_dir, config, len(specs), reset=True, db_name="reset.sqlite"
    )
    reset_manager.register_chunks(specs)
    assert done_chunks(reset_manager) == 0
    reset_spec = reset_manager.claim_next_chunk()
    assert reset_spec is not None and reset_spec[0] == 0
    reset_manager.close()


def test_checkpoint_corruption_requires_reset(temp_dir: Path):
    db_path = temp_dir / "corrupt.sqlite"
    db_path.write_bytes(b"garbled content")
    config = Config()
    with pytest.raises(sqlite3.DatabaseError):
        build_manager(temp_dir, config, chunks=1, db_name="corrupt.sqlite")

    reset_manager = build_manager(
        temp_dir, config, chunks=1, reset=True, db_name="corrupt.sqlite"
    )
    reset_manager.register_chunks(make_specs(1))
    assert reset_manager.claim_next_chunk() is not None
    reset_manager.close()


def test_concurrent_claims_block_on_lock(temp_dir: Path):
    config = Config()
    specs = make_specs(1)
    manager1 = build_manager(temp_dir, config, len(specs), db_name="concurrency.sqlite")
    manager1.register_chunks(specs)
    manager2 = build_manager(temp_dir, config, len(specs), db_name="concurrency.sqlite")
    manager2.register_chunks(specs)

    assert manager2._conn is not None
    manager2._conn.execute("PRAGMA busy_timeout=100")

    assert manager1._conn is not None
    try:
        manager1._conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            manager2.claim_next_chunk()
    finally:
        manager1._conn.execute("ROLLBACK")

    manager1.close()
    manager2.close()
