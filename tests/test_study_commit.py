"""Manifest reconciliation and crash-safe academic asset commit tests."""

from __future__ import annotations

import ast
import errno
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from study.adapters.filesystem.manifest_repository import (
    LocalFileSystem,
    ManifestRepository,
    canonical_manifest_bytes,
)
from study.adapters.filesystem.safe_committer import SafeCommitter
from study.application.ports.commit import (
    MISSING_MANIFEST_FINGERPRINT,
    AssetCommitStatus,
    ClassPackageManifest,
    CommitAssetsCommand,
    CommitCancelled,
    ConflictResolution,
    DestinationChanged,
    FileAsset,
    IntegrityError,
    LinkAsset,
    ManifestChanged,
    PlannedFile,
    ReconciliationRequired,
    SourceChanged,
    UnresolvedConflict,
    normalize_link_url,
)
from study.application.use_cases.commit_assets import CommitAssets

NOW = "2026-08-17T14:00:00Z"
LATER = "2026-08-17T15:00:00Z"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_asset(
    content: bytes,
    *,
    asset_id: str = "file-1",
    filename: str = "2026-08-17 - lesson.pdf",
) -> FileAsset:
    return FileAsset(
        id=asset_id,
        filename=filename,
        kind="material",
        media_type="application/pdf",
        size=len(content),
        sha256=digest(content),
        original_filename="lesson.pdf",
        status="ready",
    )


def roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source_root = tmp_path / "inbox"
    library_root = tmp_path / "nextcloud-library"
    staging_root = tmp_path / "state"
    package = library_root / "Distributed Systems" / "2026-08-17"
    source_root.mkdir()
    package.mkdir(parents=True)
    staging_root.mkdir()
    return source_root, library_root, staging_root, package


def stack(
    library_root: Path,
    staging_root: Path,
    *,
    filesystem: LocalFileSystem | None = None,
    fault: Any = None,
) -> tuple[ManifestRepository, CommitAssets]:
    fs = filesystem or LocalFileSystem()
    manifests = ManifestRepository(fs, fault=fault)
    committer = SafeCommitter(
        manifests,
        source_root=str(library_root.parent / "inbox"),
        library_root=str(library_root),
        staging_root=str(staging_root),
        filesystem=fs,
        fault=fault,
    )
    return manifests, CommitAssets(manifests, committer)


def command_for(
    package: Path,
    source: Path,
    asset: FileAsset,
    *,
    base: ClassPackageManifest | None = None,
    fingerprint: str = MISSING_MANIFEST_FINGERPRINT,
    conflict: ConflictResolution | None = None,
    expected_destination_sha256: str | None = None,
    links: tuple[LinkAsset, ...] = (),
    updated_at: str = NOW,
) -> CommitAssetsCommand:
    return CommitAssetsCommand(
        package_dir=str(package),
        course="Distributed Systems",
        class_date="2026-08-17",
        updated_at=updated_at,
        expected_manifest_fingerprint=fingerprint,
        base_manifest=base,
        files=(
            PlannedFile(
                asset=asset,
                source_path=str(source),
                conflict=conflict,
                expected_destination_sha256=expected_destination_sha256,
            ),
        ),
        links=links,
    )


def install_base(
    manifests: ManifestRepository,
    package: Path,
    asset: FileAsset,
) -> tuple[ClassPackageManifest, str]:
    base = ClassPackageManifest(
        course="Distributed Systems",
        class_date="2026-08-17",
        updated_at=NOW,
        assets=(asset,),
    )
    manifests.commit(str(package), base, MISSING_MANIFEST_FINGERPRINT)
    return base, manifests.read(str(package)).fingerprint


def test_canonical_manifest_commits_files_and_normalized_links(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    content = b"canonical material"
    source = source_root / "lesson.pdf"
    source.write_bytes(content)
    asset = file_asset(content)
    link = LinkAsset(
        id="link-1",
        url=normalize_link_url("HTTPS://Example.COM:443/notes?b=2&a=1#section"),
        label="Course notes",
    )
    manifests, use_case = stack(library_root, staging_root)

    result = use_case.execute(command_for(package, source, asset, links=(link,)))

    assert result.outcomes[0].status == AssetCommitStatus.PUBLISHED
    assert result.deleted_sources == (str(source),)
    assert not source.exists()
    assert (package / asset.filename).read_bytes() == content
    raw = (package / "class-package.json").read_bytes()
    assert raw == canonical_manifest_bytes(manifests.read(str(package)).manifest)
    assert str(source_root).encode() not in raw
    assert b"credential" not in raw and b"token" not in raw
    payload = json.loads(raw)
    assert payload["version"] == 1
    assert payload["assets"][1] == {
        "id": "link-1",
        "label": "Course notes",
        "type": "link",
        "url": "https://example.com/notes?a=1&b=2",
    }
    assert result.manifest_fingerprint == manifests.read(str(package)).fingerprint


def test_manifest_values_reject_paths_credentials_and_unnormalized_urls():
    content = b"x"
    with pytest.raises(ValueError, match="path component"):
        file_asset(content, filename="/tmp/secret.pdf")
    with pytest.raises(ValueError, match="credentials"):
        normalize_link_url("https://user:password@example.com/notes")
    with pytest.raises(ValueError, match="credential-like"):
        normalize_link_url("https://example.com/notes?access_token=secret")
    with pytest.raises(ValueError, match="credential-like"):
        normalize_link_url("https://example.com/notes?X-Amz-Signature=secret")
    with pytest.raises(ValueError, match="control"):
        LinkAsset(id="link-1", url="https://example.com/", label="Notes\nsecret")
    with pytest.raises(ValueError, match="normalized"):
        LinkAsset(id="link-1", url="HTTPS://EXAMPLE.COM", label="Notes")


def test_identical_destination_deduplicates_only_after_hash_verification(
    tmp_path: Path,
):
    source_root, library_root, staging_root, package = roots(tmp_path)
    content = b"same bytes"
    source = source_root / "lesson.pdf"
    source.write_bytes(content)
    asset = file_asset(content)
    (package / asset.filename).write_bytes(content)
    _, use_case = stack(library_root, staging_root)

    result = use_case.execute(command_for(package, source, asset))

    assert result.outcomes[0].status == AssetCommitStatus.DEDUPLICATED
    assert not source.exists()
    assert (package / asset.filename).read_bytes() == content


def test_different_destination_requires_explicit_resolution(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    asset = file_asset(b"new")
    destination = package / asset.filename
    destination.write_bytes(b"old")
    _, use_case = stack(library_root, staging_root)

    with pytest.raises(UnresolvedConflict):
        use_case.execute(command_for(package, source, asset))

    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"
    assert not (package / "class-package.json").exists()


def test_keep_both_uses_the_explicit_distinct_filename(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    old = file_asset(b"old", asset_id="old-file")
    (package / old.filename).write_bytes(b"old")
    manifests, use_case = stack(library_root, staging_root)
    base, fingerprint = install_base(manifests, package, old)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    new = file_asset(
        b"new",
        asset_id="new-file",
        filename="2026-08-17 - lesson (2).pdf",
    )

    use_case.execute(
        command_for(
            package,
            source,
            new,
            base=base,
            fingerprint=fingerprint,
            conflict=ConflictResolution.KEEP_BOTH,
            updated_at=LATER,
        )
    )

    assert (package / old.filename).read_bytes() == b"old"
    assert (package / new.filename).read_bytes() == b"new"
    assert {asset.id for asset in manifests.read(str(package)).manifest.assets} == {
        "old-file",
        "new-file",
    }


def test_skip_and_cancel_never_overwrite_or_delete_source(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    asset = file_asset(b"new")
    destination = package / asset.filename
    destination.write_bytes(b"old")
    _, use_case = stack(library_root, staging_root)

    skipped = use_case.execute(
        command_for(package, source, asset, conflict=ConflictResolution.SKIP)
    )
    assert skipped.outcomes[0].status == AssetCommitStatus.SKIPPED
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"

    with pytest.raises(CommitCancelled):
        use_case.execute(
            command_for(package, source, asset, conflict=ConflictResolution.CANCEL)
        )
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"


def test_nextcloud_conflicted_copy_blocks_before_staging(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    asset = file_asset(b"new")
    (package / "class-package (conflicted copy 2026-08-17).json").write_text("{}")
    _, use_case = stack(library_root, staging_root)

    with pytest.raises(ReconciliationRequired):
        use_case.execute(command_for(package, source, asset))

    assert source.exists()
    assert not (package / asset.filename).exists()
    assert not any(staging_root.iterdir())


def test_stale_manifest_fingerprint_blocks_the_plan(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    old = file_asset(b"old", asset_id="old-file")
    manifests, use_case = stack(library_root, staging_root)
    base, fingerprint = install_base(manifests, package, old)
    changed = ClassPackageManifest(
        course=base.course,
        class_date=base.class_date,
        updated_at=LATER,
        assets=base.assets,
    )
    manifests.commit(str(package), changed, fingerprint)
    source = source_root / "new.pdf"
    source.write_bytes(b"new")
    new = file_asset(b"new", asset_id="new-file", filename="2026-08-17 - new.pdf")

    with pytest.raises(ManifestChanged):
        use_case.execute(
            command_for(
                package,
                source,
                new,
                base=base,
                fingerprint=fingerprint,
                updated_at=LATER,
            )
        )

    assert source.exists()
    assert not (package / new.filename).exists()


def test_manifest_race_after_asset_publish_retains_source(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    asset = file_asset(b"new")
    external = ClassPackageManifest(
        course="Distributed Systems",
        class_date="2026-08-17",
        updated_at=LATER,
    )

    def race(point: str, context: dict[str, str]) -> None:
        if point == "before_manifest_replace":
            Path(context["path"]).write_bytes(canonical_manifest_bytes(external))

    _, use_case = stack(library_root, staging_root, fault=race)

    with pytest.raises(ManifestChanged):
        use_case.execute(command_for(package, source, asset))

    assert source.read_bytes() == b"new"
    assert (package / asset.filename).read_bytes() == b"new"


def test_source_mutation_during_stage_aborts_without_publishing(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"planned")
    asset = file_asset(b"planned")

    def mutate(point: str, _context: dict[str, str]) -> None:
        if point == "stage_file_fsynced":
            source.write_bytes(b"changed")

    _, use_case = stack(library_root, staging_root, fault=mutate)

    with pytest.raises(SourceChanged):
        use_case.execute(command_for(package, source, asset))

    assert source.read_bytes() == b"changed"
    assert not (package / asset.filename).exists()
    assert not (package / "class-package.json").exists()


def test_source_mutation_before_delete_is_retained(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"planned")
    asset = file_asset(b"planned")

    def mutate(point: str, _context: dict[str, str]) -> None:
        if point == "before_source_delete":
            source.write_bytes(b"new source version")

    manifests, use_case = stack(library_root, staging_root, fault=mutate)
    result = use_case.execute(command_for(package, source, asset))

    assert result.retained_sources == (str(source),)
    assert source.read_bytes() == b"new source version"
    assert (package / asset.filename).read_bytes() == b"planned"
    assert manifests.read(str(package)).manifest.assets == (asset,)


def test_destination_is_rehashed_before_source_deletion(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"planned")
    asset = file_asset(b"planned")

    def mutate(point: str, _context: dict[str, str]) -> None:
        if point == "before_source_delete":
            (package / asset.filename).write_bytes(b"destination race")

    _, use_case = stack(library_root, staging_root, fault=mutate)

    with pytest.raises(IntegrityError, match="before source deletion"):
        use_case.execute(command_for(package, source, asset))

    assert source.read_bytes() == b"planned"
    assert (package / "class-package.json").exists()


def test_same_id_replacement_uses_base_hash_as_guard(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    old = file_asset(b"old")
    destination = package / old.filename
    destination.write_bytes(b"old")
    manifests, use_case = stack(library_root, staging_root)
    base, fingerprint = install_base(manifests, package, old)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    replacement = file_asset(b"new")

    use_case.execute(
        command_for(
            package,
            source,
            replacement,
            base=base,
            fingerprint=fingerprint,
            conflict=ConflictResolution.REPLACE,
            updated_at=LATER,
        )
    )

    assert destination.read_bytes() == b"new"
    assert not source.exists()
    assert manifests.read(str(package)).manifest.assets == (replacement,)


def test_destination_race_does_not_silently_overwrite(tmp_path: Path):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"planned")
    asset = file_asset(b"planned")

    def race(point: str, context: dict[str, str]) -> None:
        if point == "before_asset_publish":
            Path(context["path"]).write_bytes(b"racer")

    _, use_case = stack(library_root, staging_root, fault=race)

    with pytest.raises(DestinationChanged):
        use_case.execute(command_for(package, source, asset))

    assert source.read_bytes() == b"planned"
    assert (package / asset.filename).read_bytes() == b"racer"
    assert not (package / "class-package.json").exists()


class OperationFailingFileSystem(LocalFileSystem):
    def __init__(self, operation: str, error: OSError) -> None:
        self.operation = operation
        self.error = error

    def copy_fsynced(self, source: str | Path, destination: str | Path) -> None:
        if self.operation == "copy":
            raise self.error
        super().copy_fsynced(source, destination)

    def replace(self, source: str | Path, destination: str | Path) -> None:
        if self.operation == "replace-stage" and Path(source).suffix == ".part":
            raise self.error
        super().replace(source, destination)

    def link(self, source: str | Path, destination: str | Path) -> None:
        if self.operation == "link":
            raise self.error
        super().link(source, destination)

    def fsync_directory(self, path: str | Path) -> None:
        if self.operation == "fsync-stage" and Path(path).name.startswith(
            "study-commit-"
        ):
            raise self.error
        super().fsync_directory(path)


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("replace-stage", OSError(errno.EXDEV, "cross-device link")),
        ("copy", OSError(errno.ENOSPC, "no space left")),
        ("fsync-stage", OSError(errno.EIO, "fsync failed")),
        ("link", PermissionError(errno.EACCES, "permission denied")),
    ],
)
def test_filesystem_failures_never_delete_source(
    tmp_path: Path, operation: str, error: OSError
):
    source_root, library_root, staging_root, package = roots(tmp_path)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"planned")
    asset = file_asset(b"planned")
    filesystem = OperationFailingFileSystem(operation, error)
    _, use_case = stack(library_root, staging_root, filesystem=filesystem)

    with pytest.raises(OSError) as raised:
        use_case.execute(command_for(package, source, asset))

    assert raised.value.errno == error.errno
    assert source.read_bytes() == b"planned"
    assert not (package / "class-package.json").exists()


class Crash(RuntimeError):
    pass


class CrashOnce:
    def __init__(self, point: str) -> None:
        self.point = point
        self.triggered = False

    def __call__(self, point: str, _context: dict[str, str]) -> None:
        if point == self.point and not self.triggered:
            self.triggered = True
            raise Crash(point)


@pytest.mark.parametrize(
    "crash_point",
    ["destination_backed_up", "asset_published", "manifest_replaced"],
)
def test_replacement_crashes_converge_without_source_loss(
    tmp_path: Path, crash_point: str
):
    source_root, library_root, staging_root, package = roots(tmp_path)
    old = file_asset(b"old", asset_id="old-file")
    destination = package / old.filename
    destination.write_bytes(b"old")
    initial_manifests, _ = stack(library_root, staging_root)
    base, fingerprint = install_base(initial_manifests, package, old)
    source = source_root / "lesson.pdf"
    source.write_bytes(b"new")
    new = file_asset(b"new", asset_id="new-file")
    command = command_for(
        package,
        source,
        new,
        base=base,
        fingerprint=fingerprint,
        conflict=ConflictResolution.REPLACE,
        expected_destination_sha256=old.sha256,
        updated_at=LATER,
    )
    crash = CrashOnce(crash_point)
    _, failing_use_case = stack(library_root, staging_root, fault=crash)

    with pytest.raises(Crash, match=crash_point):
        failing_use_case.execute(command)
    assert source.read_bytes() == b"new"

    manifests, recovering_use_case = stack(library_root, staging_root)
    result = recovering_use_case.execute(command)

    assert result.manifest_fingerprint == manifests.read(str(package)).fingerprint
    assert destination.read_bytes() == b"new"
    assert not source.exists()
    assert manifests.read(str(package)).manifest.assets == (new,)
    assert not any("study-backup" in child.name for child in package.iterdir())
    assert not any("study-tmp" in child.name for child in package.iterdir())
    assert not any(staging_root.iterdir())


def test_application_layer_has_no_filesystem_or_ui_imports():
    root = Path(__file__).parents[1] / "study" / "application"
    forbidden = {"os", "pathlib", "sqlite3", "textual"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & forbidden, f"forbidden application import in {path}"


def test_schema_is_strict_v1_and_covers_both_asset_shapes():
    schema_path = (
        Path(__file__).parents[1]
        / "study"
        / "contracts"
        / "schemas"
        / "class-package.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    assert schema["properties"]["version"] == {"const": 1}
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["fileAsset"]["additionalProperties"] is False
    assert schema["$defs"]["linkAsset"]["additionalProperties"] is False
