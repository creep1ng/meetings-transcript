"""Executable composition and fake-worker study workflow tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from study.application.ports.commit import LinkAsset
from study.application.ports.jobs import JobStatus
from study.bootstrap import build_study_app
from study.config import AcademicConfig
from study.presentation.textual.ports import MoveConflictError

FAKE_WORKER = Path(__file__).parent / "fixtures" / "fake_study_worker.py"


@pytest.mark.asyncio
async def test_fake_worker_transcribes_and_commits_complete_class_package(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    inbox = source_root / "Downloads"
    library = tmp_path / "Nextcloud" / "Academic"
    state = tmp_path / "state"
    inbox.mkdir(parents=True)
    library.mkdir(parents=True)
    source = inbox / "lecture.mp3"
    source.write_bytes(b"representative fake audio")
    config = AcademicConfig(
        library_root=str(library),
        source_root=str(source_root),
        start_path=str(inbox),
        state_root=str(state),
        worker_command=(sys.executable, str(FAKE_WORKER)),
    )
    app = build_study_app(config)
    facade = app.facade

    try:
        catalog = await facade.browse_source(None)
        assert catalog.location == "Downloads"
        assert catalog.entries[0].location == "Downloads/lecture.mp3"
        location = catalog.entries[0].location

        course = await facade.create_course("Distributed Systems")
        assert course.name == "Distributed Systems"
        await facade.assign_sources((location,), "Distributed Systems", "2026-08-17")
        await facade.dispatch_transcriptions((location,), "es")

        for _ in range(200):
            jobs = await facade.list_jobs()
            if jobs and jobs[0].status == JobStatus.READY_TO_COMMIT.value:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("fake transcription did not become ready to commit")

        move_result = await facade.move_sources(
            (location,), "Distributed Systems", "2026-08-17"
        )
        assert move_result.moved_locations == (location,)

        package = library / "Distributed Systems" / "2026-08-17"
        original = package / "2026-08-17 - lecture.mp3"
        transcript_json = package / "2026-08-17 - lecture.transcript.json"
        transcript_markdown = package / "2026-08-17 - lecture.transcript.md"
        manifest_path = package / "class-package.json"
        assert not source.exists()
        assert original.read_bytes() == b"representative fake audio"
        assert transcript_json.is_file() and transcript_markdown.is_file()
        assert "SPEAKER_00" in transcript_markdown.read_text()
        transcript = json.loads(transcript_json.read_text())
        assert transcript["assignment"] == {
            "course": "Distributed Systems",
            "class_date": "2026-08-17",
        }
        manifest = json.loads(manifest_path.read_text())
        assert {asset["filename"] for asset in manifest["assets"]} == {
            original.name,
            transcript_json.name,
            transcript_markdown.name,
        }
        assert all("/" not in asset["filename"] for asset in manifest["assets"])

        schema_root = Path(__file__).parents[1] / "study" / "contracts" / "schemas"
        Draft202012Validator(
            json.loads((schema_root / "class-package.schema.json").read_text())
        ).validate(manifest)
        Draft202012Validator(
            json.loads((schema_root / "transcript.schema.json").read_text())
        ).validate(transcript)

        stored = facade.jobs.list_jobs(include_invalidated=True)
        assert stored[0].status is JobStatus.COMPLETED
        assert facade.jobs.staging_usage(stored[0].id) == 0
    finally:
        await facade.close(cancel_active=False)


@pytest.mark.asyncio
async def test_move_reports_different_content_before_touching_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    inbox = source_root / "Downloads"
    library = tmp_path / "library"
    inbox.mkdir(parents=True)
    library.mkdir()
    source = inbox / "notes.pdf"
    source.write_bytes(b"incoming")
    config = AcademicConfig(
        library_root=str(library),
        source_root=str(source_root),
        start_path=str(inbox),
        state_root=str(tmp_path / "state"),
        worker_command=(sys.executable, str(FAKE_WORKER)),
    )
    app = build_study_app(config)
    facade = app.facade
    try:
        await facade.create_course("Algorithms")
        package = library / "Algorithms" / "2026-08-17"
        package.mkdir()
        destination = package / "2026-08-17 - notes.pdf"
        destination.write_bytes(b"existing")
        with pytest.raises(MoveConflictError) as unresolved:
            await facade.move_sources(
                ("Downloads/notes.pdf",), "Algorithms", "2026-08-17"
            )
        assert source.read_bytes() == b"incoming"
        assert destination.read_bytes() == b"existing"
        assert not (package / "class-package.json").exists()

        result = await facade.move_sources(
            ("Downloads/notes.pdf",),
            "Algorithms",
            "2026-08-17",
            {unresolved.value.conflict.resolution_key: "skip"},
        )
        assert result.moved_locations == ()
        assert result.skipped_locations == ("Downloads/notes.pdf",)
        assert source.read_bytes() == b"incoming"
        assert destination.read_bytes() == b"existing"
    finally:
        await facade.close(cancel_active=False)


@pytest.mark.asyncio
async def test_add_link_normalizes_manifest_asset_and_refreshes_package_view(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    library = tmp_path / "library"
    source_root.mkdir()
    library.mkdir()
    config = AcademicConfig(
        library_root=str(library),
        source_root=str(source_root),
        start_path=str(source_root),
        state_root=str(tmp_path / "state"),
        worker_command=(sys.executable, str(FAKE_WORKER)),
    )
    app = build_study_app(config)
    facade = app.facade
    try:
        await facade.create_course("Algorithms")
        await facade.add_link(
            "HTTPS://GitHub.COM:443/example/repo?b=2&a=1#readme",
            "Course repository",
            "Algorithms",
            "2026-08-17",
        )

        package = library / "Algorithms" / "2026-08-17"
        manifest = facade.manifests.read(str(package)).manifest
        assert manifest is not None
        assert len(manifest.assets) == 1
        link = manifest.assets[0]
        assert isinstance(link, LinkAsset)
        assert link.url == "https://github.com/example/repo?a=1&b=2"
        assert link.label == "Course repository"

        packages = await facade.list_packages("Algorithms")
        assert packages[0].asset_names == (
            "Course repository [link] https://github.com/example/repo?a=1&b=2",
        )
    finally:
        await facade.close(cancel_active=False)


@pytest.mark.asyncio
async def test_each_colliding_derivative_requires_its_own_resolution(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    inbox = source_root / "Downloads"
    library = tmp_path / "library"
    inbox.mkdir(parents=True)
    library.mkdir()
    source = inbox / "lecture.mp3"
    source.write_bytes(b"incoming audio")
    config = AcademicConfig(
        library_root=str(library),
        source_root=str(source_root),
        start_path=str(inbox),
        state_root=str(tmp_path / "state"),
        worker_command=(sys.executable, str(FAKE_WORKER)),
    )
    app = build_study_app(config)
    facade = app.facade
    location = "Downloads/lecture.mp3"
    try:
        await facade.create_course("Algorithms")
        await facade.assign_sources((location,), "Algorithms", "2026-08-17")
        await facade.dispatch_transcriptions((location,), "es")
        for _ in range(200):
            jobs = await facade.list_jobs()
            if jobs and jobs[0].status == JobStatus.READY_TO_COMMIT.value:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("fake transcription did not become ready to commit")

        with pytest.raises(RuntimeError, match="Generated assets changed after review"):
            await facade.move_sources(
                (location,),
                "Algorithms",
                "2026-08-17",
                expected_generated_assets={location: ()},
            )
        assert source.read_bytes() == b"incoming audio"

        source.write_bytes(b"changed after transcription")
        with pytest.raises(RuntimeError, match="stale transcript artifacts"):
            await facade.move_sources((location,), "Algorithms", "2026-08-17")
        source.write_bytes(b"incoming audio")

        package = library / "Algorithms" / "2026-08-17"
        package.mkdir(exist_ok=True)
        (package / "2026-08-17 - lecture.mp3").write_bytes(b"existing audio")
        (package / "2026-08-17 - lecture.transcript.json").write_bytes(
            b"existing transcript"
        )

        with pytest.raises(MoveConflictError) as original_conflict:
            await facade.move_sources((location,), "Algorithms", "2026-08-17")
        original_key = original_conflict.value.conflict.resolution_key
        assert original_conflict.value.conflict.filename.endswith("lecture.mp3")

        with pytest.raises(MoveConflictError) as transcript_conflict:
            await facade.move_sources(
                (location,),
                "Algorithms",
                "2026-08-17",
                {original_key: "replace"},
            )
        assert transcript_conflict.value.conflict.filename.endswith("transcript.json")
        assert transcript_conflict.value.conflict.resolution_key != original_key
        assert source.read_bytes() == b"incoming audio"
    finally:
        await facade.close(cancel_active=False)
