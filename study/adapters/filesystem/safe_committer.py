"""Crash-safe filesystem implementation of the asset committer port."""

from __future__ import annotations

import hashlib
from pathlib import Path

from study.adapters.filesystem.manifest_repository import (
    FaultInjector,
    FileSystem,
    LocalFileSystem,
)
from study.application.ports.commit import (
    AssetCommitOutcome,
    AssetCommitStatus,
    ClassPackageManifest,
    CommitAssetsCommand,
    CommitAssetsResult,
    CommitCancelled,
    ConflictResolution,
    DestinationChanged,
    FileAsset,
    IntegrityError,
    ManifestChanged,
    ManifestStore,
    PlannedFile,
    ReconciliationRequired,
    SourceChanged,
    UnresolvedConflict,
    UnsafePathError,
)
from study.domain.value_objects import ClassDate, CourseName


class SafeCommitter:
    """Copy, verify, publish, commit the manifest, then remove sources."""

    def __init__(
        self,
        manifests: ManifestStore,
        *,
        source_root: str,
        library_root: str,
        staging_root: str,
        filesystem: FileSystem | None = None,
        fault: FaultInjector | None = None,
    ) -> None:
        self._manifests = manifests
        repository_filesystem = getattr(manifests, "filesystem", None)
        self._fs = filesystem or repository_filesystem or LocalFileSystem()
        self._source_root = self._fs.resolve(source_root)
        self._library_root = self._fs.resolve(library_root)
        self._staging_root = self._fs.resolve(staging_root)
        self._fault = fault
        if self._inside(self._staging_root, self._library_root):
            raise UnsafePathError("staging_root must be outside the managed library")

    def commit(
        self,
        command: CommitAssetsCommand,
        manifest: ClassPackageManifest,
        files: tuple[PlannedFile, ...],
    ) -> CommitAssetsResult:
        if any(item.conflict == ConflictResolution.CANCEL for item in files):
            raise CommitCancelled("asset commit cancelled before staging")

        package = self._fs.resolve(command.package_dir)
        if not self._inside(package, self._library_root):
            raise UnsafePathError("package directory is outside the managed library")
        relative_package = package.relative_to(self._library_root)
        if len(relative_package.parts) != 2:
            raise UnsafePathError("package must be one direct course and one date deep")
        course = CourseName(relative_package.parts[0])
        class_date = ClassDate.parse(relative_package.parts[1])
        if course.value != command.course or class_date.iso != command.class_date:
            raise UnsafePathError("package path does not match the planned course/date")
        for item in files:
            source = self._fs.resolve(item.source_path)
            required_root = (
                self._source_root if item.delete_source else self._staging_root
            )
            if not self._inside(source, required_root):
                raise UnsafePathError(
                    "asset source is outside its configured source/staging root"
                )
            if self._inside(source, self._library_root):
                raise UnsafePathError("managed library files cannot be move sources")

        self._fs.mkdir(package)
        target_fingerprint = self._manifests.fingerprint(manifest)
        current = self._manifests.read(str(package))
        if current.conflicted_copies:
            raise ReconciliationRequired(
                "Nextcloud conflicted copy requires a new scan"
            )
        if current.fingerprint not in {
            command.expected_manifest_fingerprint,
            target_fingerprint,
        }:
            raise ManifestChanged("manifest changed after the commit plan was created")

        transaction_id = target_fingerprint[:24]
        transaction_dir = self._staging_root / f"study-commit-{transaction_id}"
        if current.fingerprint == target_fingerprint:
            recovery_outcomes = tuple(
                self._confirm_published(package, item) for item in files
            )
            deleted, retained = self._finish(package, files, transaction_id)
            if self._fs.exists(transaction_dir):
                self._fs.remove_tree(transaction_dir)
            return CommitAssetsResult(
                target_fingerprint, recovery_outcomes, deleted, retained
            )

        self._preflight(package, files, transaction_id)
        self._verify_existing_assets(package, manifest, files)
        self._fs.mkdir(transaction_dir)

        staged: dict[str, Path] = {}
        for item in files:
            destination = package / item.asset.filename
            if self._fs.exists(destination) and self._verified(destination, item):
                continue
            staged[item.asset.id] = self._stage(transaction_dir, item)

        outcomes: list[AssetCommitOutcome] = []
        for item in files:
            destination = package / item.asset.filename
            if self._fs.exists(destination) and self._verified(destination, item):
                outcomes.append(self._outcome(item, AssetCommitStatus.DEDUPLICATED))
                continue
            self._publish(package, staged[item.asset.id], item, transaction_id)
            outcomes.append(self._outcome(item, AssetCommitStatus.PUBLISHED))

        manifest_fingerprint = self._manifests.commit(
            str(package), manifest, command.expected_manifest_fingerprint
        )
        deleted, retained = self._finish(package, files, transaction_id)
        if self._fs.exists(transaction_dir):
            self._fs.remove_tree(transaction_dir)
        return CommitAssetsResult(
            manifest_fingerprint, tuple(outcomes), deleted, retained
        )

    def _verify_existing_assets(
        self,
        package: Path,
        manifest: ClassPackageManifest,
        files: tuple[PlannedFile, ...],
    ) -> None:
        active_ids = {item.asset.id for item in files}
        for asset in manifest.assets:
            if not isinstance(asset, FileAsset) or asset.id in active_ids:
                continue
            destination = package / asset.filename
            if (
                not self._fs.is_file(destination)
                or self._fs.size(destination) != asset.size
                or self._fs.sha256(destination) != asset.sha256
            ):
                raise IntegrityError(
                    f"existing package asset changed before manifest commit: {asset.filename}"
                )

    def _preflight(
        self, package: Path, files: tuple[PlannedFile, ...], transaction_id: str
    ) -> None:
        for item in files:
            source = self._recover_source(item, transaction_id)
            if not self._fs.is_file(source) or not self._verified(source, item):
                raise SourceChanged(
                    f"source changed or disappeared: {item.source_path}"
                )

            destination = package / item.asset.filename
            backup = self._backup_path(package, item, transaction_id)
            if self._fs.exists(destination):
                destination_hash = self._fs.sha256(destination)
                if destination_hash == item.asset.sha256:
                    continue
                if item.conflict != ConflictResolution.REPLACE:
                    raise UnresolvedConflict(
                        f"destination exists with different content: {item.asset.filename}"
                    )
                if destination_hash != item.expected_destination_sha256:
                    raise DestinationChanged(
                        f"destination changed since planning: {item.asset.filename}"
                    )
            elif self._fs.exists(backup):
                if item.conflict != ConflictResolution.REPLACE:
                    raise DestinationChanged("unexpected replacement backup found")
                if self._fs.sha256(backup) != item.expected_destination_sha256:
                    raise DestinationChanged("replacement backup differs from the plan")
            elif item.conflict == ConflictResolution.REPLACE:
                raise DestinationChanged(
                    f"replacement destination disappeared: {item.asset.filename}"
                )

    def _stage(self, transaction_dir: Path, item: PlannedFile) -> Path:
        key = hashlib.sha256(item.asset.id.encode("utf-8")).hexdigest()[:24]
        staged = transaction_dir / f"{key}.stage"
        if self._fs.exists(staged) and self._verified(staged, item):
            return staged
        part = transaction_dir / f"{key}.part"
        source = self._recover_source(
            item, transaction_dir.name.removeprefix("study-commit-")
        )
        self._fs.copy_fsynced(source, part)
        self._hit("stage_file_fsynced", item, part)
        if not self._verified(part, item):
            raise IntegrityError(
                f"staged copy failed verification: {item.asset.filename}"
            )
        if not self._verified(source, item):
            raise SourceChanged(f"source mutated while staging: {item.source_path}")
        self._fs.replace(part, staged)
        self._fs.fsync_directory(transaction_dir)
        self._hit("stage_published", item, staged)
        return staged

    def _publish(
        self,
        package: Path,
        staged: Path,
        item: PlannedFile,
        transaction_id: str,
    ) -> None:
        destination = package / item.asset.filename
        key = hashlib.sha256(item.asset.filename.encode("utf-8")).hexdigest()[:16]
        temporary = package / f".study-tmp-{transaction_id}-{key}"
        if not (self._fs.exists(temporary) and self._verified(temporary, item)):
            self._fs.copy_fsynced(staged, temporary)
        self._hit("asset_temp_fsynced", item, temporary)
        if not self._verified(temporary, item):
            raise IntegrityError(
                f"temporary copy failed verification: {item.asset.filename}"
            )

        if item.conflict == ConflictResolution.REPLACE:
            self._backup_destination(package, item, transaction_id)
        elif self._fs.exists(destination):
            if self._verified(destination, item):
                self._fs.unlink(temporary, missing_ok=True)
                return
            raise DestinationChanged(
                f"destination appeared during publish: {item.asset.filename}"
            )

        if self._fs.exists(destination):
            if self._verified(destination, item):
                self._fs.unlink(temporary, missing_ok=True)
                return
            raise DestinationChanged(
                f"destination raced with publish: {item.asset.filename}"
            )
        self._hit("before_asset_publish", item, destination)
        try:
            self._fs.link(temporary, destination)
        except FileExistsError as error:
            if self._fs.exists(destination) and self._verified(destination, item):
                self._fs.unlink(temporary, missing_ok=True)
                return
            raise DestinationChanged(
                f"destination raced with publish: {item.asset.filename}"
            ) from error
        self._fs.unlink(temporary, missing_ok=True)
        self._fs.fsync_directory(package)
        self._hit("asset_published", item, destination)
        if not self._verified(destination, item):
            raise IntegrityError(
                f"published asset failed verification: {item.asset.filename}"
            )

    def _backup_destination(
        self, package: Path, item: PlannedFile, transaction_id: str
    ) -> None:
        destination = package / item.asset.filename
        backup = self._backup_path(package, item, transaction_id)
        if self._fs.exists(backup):
            if self._fs.sha256(backup) != item.expected_destination_sha256:
                raise DestinationChanged("replacement backup differs from the plan")
            return
        if not self._fs.exists(destination):
            raise DestinationChanged("replacement destination disappeared")
        if self._fs.sha256(destination) != item.expected_destination_sha256:
            raise DestinationChanged("replacement destination changed before backup")
        self._hit("before_destination_backup", item, destination)
        if self._fs.sha256(destination) != item.expected_destination_sha256:
            raise DestinationChanged("replacement destination raced with backup")
        self._fs.replace(destination, backup)
        self._fs.fsync_directory(package)
        self._hit("destination_backed_up", item, backup)

    def _finish(
        self, package: Path, files: tuple[PlannedFile, ...], transaction_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        for item in files:
            destination = package / item.asset.filename
            if not self._verified(destination, item):
                raise IntegrityError(
                    f"committed destination failed verification: {item.asset.filename}"
                )

        for item in files:
            backup = self._backup_path(package, item, transaction_id)
            if self._fs.exists(backup):
                self._fs.unlink(backup)
                self._fs.fsync_directory(package)

        deleted: list[str] = []
        retained: list[str] = []
        for item in files:
            if not item.delete_source:
                retained.append(item.source_path)
                continue
            source = self._recover_source(item, transaction_id)
            destination = package / item.asset.filename
            if not self._fs.exists(source):
                deleted.append(item.source_path)
                continue
            if not self._verified(source, item):
                retained.append(item.source_path)
                continue
            self._hit("before_source_delete", item, source)
            if not self._verified(destination, item):
                raise IntegrityError(
                    f"destination changed before source deletion: {item.asset.filename}"
                )
            if not self._verified(source, item):
                retained.append(item.source_path)
                continue
            quarantine = self._source_quarantine_path(item, transaction_id)
            if source != quarantine:
                self._fs.replace(source, quarantine)
                self._fs.fsync_directory(source.parent)
            self._hit("source_quarantined", item, quarantine)
            if not self._verified(quarantine, item):
                self._restore_quarantine(item, quarantine)
                retained.append(item.source_path)
                continue
            if not self._verified(destination, item):
                self._restore_quarantine(item, quarantine)
                raise IntegrityError(
                    f"destination changed before quarantined source deletion: {item.asset.filename}"
                )
            self._fs.unlink(quarantine)
            self._fs.fsync_directory(source.parent)
            if self._fs.exists(self._fs.resolve(item.source_path)):
                retained.append(item.source_path)
            else:
                deleted.append(item.source_path)
        return tuple(deleted), tuple(retained)

    def _confirm_published(
        self, package: Path, item: PlannedFile
    ) -> AssetCommitOutcome:
        destination = package / item.asset.filename
        if not self._verified(destination, item):
            raise IntegrityError(
                f"manifest is committed but destination is invalid: {item.asset.filename}"
            )
        return self._outcome(item, AssetCommitStatus.DEDUPLICATED)

    def _verified(self, path: Path, item: PlannedFile) -> bool:
        return (
            self._fs.is_file(path)
            and self._fs.size(path) == item.asset.size
            and self._fs.sha256(path) == item.asset.sha256
        )

    def _recover_source(self, item: PlannedFile, transaction_id: str) -> Path:
        source = self._fs.resolve(item.source_path)
        quarantine = self._source_quarantine_path(item, transaction_id)
        if self._fs.exists(source):
            return source
        if self._fs.exists(quarantine) and self._verified(quarantine, item):
            self._fs.replace(quarantine, source)
            self._fs.fsync_directory(source.parent)
            return source
        return source

    def _restore_quarantine(self, item: PlannedFile, quarantine: Path) -> None:
        source = self._fs.resolve(item.source_path)
        if not self._fs.exists(source) and self._fs.exists(quarantine):
            self._fs.replace(quarantine, source)
            self._fs.fsync_directory(source.parent)

    def _source_quarantine_path(self, item: PlannedFile, transaction_id: str) -> Path:
        source = self._fs.resolve(item.source_path)
        key = hashlib.sha256(item.asset.id.encode("utf-8")).hexdigest()[:16]
        return source.parent / f".study-source-{transaction_id}-{key}"

    @staticmethod
    def _outcome(item: PlannedFile, status: AssetCommitStatus) -> AssetCommitOutcome:
        return AssetCommitOutcome(item.asset.id, item.asset.filename, status)

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _backup_path(package: Path, item: PlannedFile, transaction_id: str) -> Path:
        key = hashlib.sha256(item.asset.filename.encode("utf-8")).hexdigest()[:16]
        return package / f".study-backup-{transaction_id}-{key}"

    def _hit(self, point: str, item: PlannedFile, path: Path) -> None:
        if self._fault is not None:
            self._fault(
                point,
                {"asset_id": item.asset.id, "path": str(path)},
            )
