"""Focused tests for source browsing, library catalog, and asset planning."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from study.adapters.filesystem.library_repository import (
    FilesystemLibraryRepository,
)
from study.adapters.filesystem.source_repository import FilesystemSourceRepository
from study.application.dto import BrowseSourcesQuery, PlanAssetCommand
from study.application.use_cases.browse_sources import BrowseSources
from study.application.use_cases.plan_assets import PlanAssets
from study.domain.errors import LibrarySubtreeError, NameCollisionError, RootEscapeError


def _source_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    downloads = source / "Downloads"
    library = source / "Nextcloud" / "Academic"
    outside = tmp_path / "outside"
    downloads.mkdir(parents=True)
    library.mkdir(parents=True)
    outside.mkdir()
    return source, downloads, library, outside


def test_source_browser_confines_navigation_and_rejects_escape_symlinks(tmp_path):
    source, downloads, library, outside = _source_tree(tmp_path)
    (downloads / "folder").mkdir()
    (downloads / "lecture.mp3").write_bytes(b"audio")
    (downloads / "escape").symlink_to(outside, target_is_directory=True)
    repository = FilesystemSourceRepository(source, library, start_path=downloads)

    result = BrowseSources(repository).execute(BrowseSourcesQuery())
    assert result.location == "Downloads"
    assert result.parent_location == ""
    assert [entry.name for entry in result.entries] == ["folder", "lecture.mp3"]
    assert result.entries[1].asset_kind == "audio"
    with pytest.raises(RootEscapeError):
        repository.browse("../outside")
    with pytest.raises(RootEscapeError):
        repository.browse("Downloads/escape")


def test_source_browser_excludes_library_and_filters_hidden_incomplete(tmp_path):
    source, downloads, library, _ = _source_tree(tmp_path)
    (downloads / ".hidden.pdf").touch()
    (downloads / "recording.mp4.part").touch()
    (downloads / "visible.xyz").touch()
    repository = FilesystemSourceRepository(source, library, start_path=downloads)

    assert [entry.name for entry in repository.browse("Downloads").entries] == [
        "visible.xyz"
    ]
    assert [entry.name for entry in repository.browse("").entries] == [
        "Downloads",
        "Nextcloud",
    ]
    assert repository.browse("Nextcloud").entries == ()
    with pytest.raises(LibrarySubtreeError):
        repository.browse("Nextcloud/Academic")

    unfiltered = FilesystemSourceRepository(
        source,
        library,
        start_path=downloads,
        show_hidden=True,
        include_incomplete=True,
    )
    assert {entry.name for entry in unfiltered.browse("Downloads").entries} == {
        ".hidden.pdf",
        "recording.mp4.part",
        "visible.xyz",
    }


def test_library_catalog_uses_only_safe_direct_child_courses(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "Math").mkdir()
    (library / "Math" / "2026-08-17").mkdir()
    (library / "Math" / "not-a-package").mkdir()
    (library / "CON").mkdir()
    (library / ".hidden").mkdir()
    (library / "container" / "Nested Course").mkdir(parents=True)
    repository = FilesystemLibraryRepository(library)

    courses = repository.list_courses()
    assert [(course.name, course.package_count) for course in courses] == [
        ("container", 0),
        ("Math", 1),
    ]


def test_course_creation_normalizes_nfc_and_rejects_casefold_collisions(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    repository = FilesystemLibraryRepository(library)
    created = repository.create_course("Cafe\u0301")
    assert created.name == "Café"
    assert (library / "Café").is_dir()

    repository.create_course("Straße")
    with pytest.raises(NameCollisionError):
        repository.create_course("STRASSE")


def test_existing_nfc_equivalent_course_names_are_ambiguous(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "Café").mkdir()
    (library / "Cafe\u0301").mkdir()
    with pytest.raises(NameCollisionError):
        FilesystemLibraryRepository(library).list_courses()


def test_existing_date_package_is_adopted_without_renaming_assets(tmp_path):
    library = tmp_path / "library"
    package = library / "Math" / "2026-08-17"
    package.mkdir(parents=True)
    original = package / "Original Lecture.MP3"
    original.write_bytes(b"audio")
    (package / "notes.pdf.part").touch()
    (package / ".hidden.txt").touch()
    (package / "class-package.json").write_text("{}")
    repository = FilesystemLibraryRepository(library)

    packages = repository.list_packages("math")
    assert len(packages) == 1
    assert packages[0].adopted
    assert packages[0].has_manifest
    assert [(asset.name, asset.kind) for asset in packages[0].assets] == [
        ("Original Lecture.MP3", "audio")
    ]
    repository.ensure_package("Math", "2026-08-17")
    assert original.exists()
    assert not (package / "2026-08-17 - Original Lecture.MP3").exists()


def test_package_catalog_is_newest_first_and_ensure_creates_iso_directory(tmp_path):
    library = tmp_path / "library"
    (library / "Math" / "2026-01-01").mkdir(parents=True)
    repository = FilesystemLibraryRepository(library)
    created = repository.ensure_package("Math", "2026-08-17")
    assert created.class_date == "2026-08-17"
    assert [package.class_date for package in repository.list_packages("Math")] == [
        "2026-08-17",
        "2026-01-01",
    ]


def test_asset_planning_uses_strings_prefixes_overrides_and_immutable_dtos():
    planner = PlanAssets()
    plan = planner.execute(
        PlanAssetCommand(
            course_name="Math",
            class_date="2026-08-17",
            source_location="Downloads/notes.bin",
            original_name="notes.bin",
            kind_override="document",
        )
    )
    assert plan.kind == "document"
    assert plan.destination_name == "2026-08-17 - notes.bin"
    assert plan.destination_location == "Math/2026-08-17/2026-08-17 - notes.bin"
    with pytest.raises(FrozenInstanceError):
        plan.kind = "other"


def test_link_planning_normalizes_url_and_creates_no_file_destination():
    plan = PlanAssets()(
        PlanAssetCommand(
            course_name="Math",
            class_date="2026-08-17",
            url="HTTPS://GitHub.COM/example/course",
            label="Course repository",
        )
    )
    assert plan.kind == "link"
    assert plan.source_url == "https://github.com/example/course"
    assert plan.destination_name is None
    assert plan.destination_location is None


def test_application_layer_has_no_pathlib_or_textual_imports():
    application = Path(__file__).parents[1] / "study" / "application"
    for module in application.rglob("*.py"):
        source = module.read_text()
        assert "pathlib" not in source, module
        assert "textual" not in source.casefold(), module
