"""Canonical v1 class-package manifest persistence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from study.application.ports.commit import (
    MISSING_MANIFEST_FINGERPRINT,
    ClassPackageManifest,
    FileAsset,
    IntegrityError,
    LinkAsset,
    ManifestChanged,
    ManifestSnapshot,
    ReconciliationRequired,
)

FaultInjector = Callable[[str, Mapping[str, str]], None]


class FileSystem(Protocol):
    def resolve(self, path: str | Path) -> Path: ...

    def exists(self, path: str | Path) -> bool: ...

    def is_file(self, path: str | Path) -> bool: ...

    def mkdir(self, path: str | Path) -> None: ...

    def list_names(self, path: str | Path) -> tuple[str, ...]: ...

    def read_bytes(self, path: str | Path) -> bytes: ...

    def write_fsynced(self, path: str | Path, data: bytes) -> None: ...

    def copy_fsynced(self, source: str | Path, destination: str | Path) -> None: ...

    def sha256(self, path: str | Path) -> str: ...

    def size(self, path: str | Path) -> int: ...

    def replace(self, source: str | Path, destination: str | Path) -> None: ...

    def link(self, source: str | Path, destination: str | Path) -> None: ...

    def unlink(self, path: str | Path, *, missing_ok: bool = False) -> None: ...

    def remove_tree(self, path: str | Path) -> None: ...

    def fsync_directory(self, path: str | Path) -> None: ...


class LocalFileSystem:
    """Small injectable surface containing every mutating filesystem primitive."""

    def resolve(self, path: str | Path) -> Path:
        return Path(path).expanduser().resolve(strict=False)

    def exists(self, path: str | Path) -> bool:
        return Path(path).exists()

    def is_file(self, path: str | Path) -> bool:
        return Path(path).is_file()

    def mkdir(self, path: str | Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def list_names(self, path: str | Path) -> tuple[str, ...]:
        directory = Path(path)
        if not directory.exists():
            return ()
        return tuple(child.name for child in directory.iterdir())

    def read_bytes(self, path: str | Path) -> bytes:
        return Path(path).read_bytes()

    def write_fsynced(self, path: str | Path, data: bytes) -> None:
        with Path(path).open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def copy_fsynced(self, source: str | Path, destination: str | Path) -> None:
        with (
            Path(source).open("rb") as source_handle,
            Path(destination).open("wb") as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())

    def sha256(self, path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def size(self, path: str | Path) -> int:
        return Path(path).stat().st_size

    def replace(self, source: str | Path, destination: str | Path) -> None:
        os.replace(source, destination)

    def link(self, source: str | Path, destination: str | Path) -> None:
        os.link(source, destination)

    def unlink(self, path: str | Path, *, missing_ok: bool = False) -> None:
        Path(path).unlink(missing_ok=missing_ok)

    def remove_tree(self, path: str | Path) -> None:
        shutil.rmtree(path, ignore_errors=False)

    def fsync_directory(self, path: str | Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _asset_to_dict(asset: FileAsset | LinkAsset) -> dict[str, object]:
    if isinstance(asset, FileAsset):
        return {
            "derived_from": asset.derived_from,
            "filename": asset.filename,
            "id": asset.id,
            "kind": asset.kind,
            "media_type": asset.media_type,
            "original_filename": asset.original_filename,
            "sha256": asset.sha256,
            "size": asset.size,
            "status": asset.status,
            "type": "file",
        }
    return {"id": asset.id, "label": asset.label, "type": "link", "url": asset.url}


def manifest_to_dict(manifest: ClassPackageManifest) -> dict[str, object]:
    return {
        "assets": [_asset_to_dict(asset) for asset in manifest.assets],
        "class_date": manifest.class_date,
        "course": manifest.course,
        "updated_at": manifest.updated_at,
        "version": manifest.version,
    }


def canonical_manifest_bytes(manifest: ClassPackageManifest) -> bytes:
    return (
        json.dumps(
            manifest_to_dict(manifest),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _require_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise IntegrityError(
            f"{context} fields differ: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise IntegrityError(f"{field} must be a string")
    return value


def manifest_from_dict(value: object) -> ClassPackageManifest:
    if not isinstance(value, dict):
        raise IntegrityError("manifest root must be an object")
    _require_keys(
        value, {"version", "course", "class_date", "updated_at", "assets"}, "manifest"
    )
    if not isinstance(value["assets"], list):
        raise IntegrityError("manifest assets must be an array")

    assets: list[FileAsset | LinkAsset] = []
    for raw in value["assets"]:
        if not isinstance(raw, dict):
            raise IntegrityError("manifest asset must be an object")
        if raw.get("type") == "file":
            _require_keys(
                raw,
                {
                    "derived_from",
                    "filename",
                    "id",
                    "kind",
                    "media_type",
                    "original_filename",
                    "sha256",
                    "size",
                    "status",
                    "type",
                },
                "file asset",
            )
            if isinstance(raw["size"], bool) or not isinstance(raw["size"], int):
                raise IntegrityError("file asset size must be an integer")
            assets.append(
                FileAsset(
                    id=_require_string(raw["id"], "file asset id"),
                    filename=_require_string(raw["filename"], "file asset filename"),
                    kind=_require_string(raw["kind"], "file asset kind"),
                    media_type=_require_string(
                        raw["media_type"], "file asset media_type"
                    ),
                    size=raw["size"],
                    sha256=_require_string(raw["sha256"], "file asset sha256"),
                    original_filename=_require_string(
                        raw["original_filename"], "file asset original_filename"
                    ),
                    status=_require_string(raw["status"], "file asset status"),
                    derived_from=(
                        None
                        if raw["derived_from"] is None
                        else _require_string(
                            raw["derived_from"], "file asset derived_from"
                        )
                    ),
                )
            )
        elif raw.get("type") == "link":
            _require_keys(raw, {"id", "label", "type", "url"}, "link asset")
            assets.append(
                LinkAsset(
                    id=_require_string(raw["id"], "link asset id"),
                    url=_require_string(raw["url"], "link asset url"),
                    label=_require_string(raw["label"], "link asset label"),
                )
            )
        else:
            raise IntegrityError("manifest asset type must be file or link")

    try:
        return ClassPackageManifest(
            version=value["version"],
            course=_require_string(value["course"], "course"),
            class_date=_require_string(value["class_date"], "class_date"),
            updated_at=_require_string(value["updated_at"], "updated_at"),
            assets=tuple(assets),
        )
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"invalid manifest: {error}") from error


class ManifestRepository:
    MANIFEST_NAME = "class-package.json"

    def __init__(
        self,
        filesystem: FileSystem | None = None,
        fault: FaultInjector | None = None,
    ) -> None:
        self.filesystem = filesystem or LocalFileSystem()
        self._fault = fault

    def fingerprint(self, manifest: ClassPackageManifest) -> str:
        return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()

    def read(self, package_dir: str) -> ManifestSnapshot:
        directory = self.filesystem.resolve(package_dir)
        conflicted = tuple(
            sorted(
                name
                for name in self.filesystem.list_names(directory)
                if "conflicted copy" in name.casefold()
            )
        )
        path = directory / self.MANIFEST_NAME
        if not self.filesystem.exists(path):
            return ManifestSnapshot(None, MISSING_MANIFEST_FINGERPRINT, conflicted)
        try:
            raw = self.filesystem.read_bytes(path)
            decoded = json.loads(raw)
            manifest = manifest_from_dict(decoded)
        except IntegrityError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrityError(f"cannot read canonical manifest: {error}") from error
        return ManifestSnapshot(
            manifest=manifest,
            fingerprint=hashlib.sha256(raw).hexdigest(),
            conflicted_copies=conflicted,
        )

    def commit(
        self,
        package_dir: str,
        manifest: ClassPackageManifest,
        expected_fingerprint: str,
    ) -> str:
        directory = self.filesystem.resolve(package_dir)
        self.filesystem.mkdir(directory)
        target_bytes = canonical_manifest_bytes(manifest)
        target_fingerprint = hashlib.sha256(target_bytes).hexdigest()
        current = self.read(str(directory))
        self._assert_reconcilable(current, expected_fingerprint, target_fingerprint)
        if current.fingerprint == target_fingerprint:
            self.filesystem.fsync_directory(directory)
            return target_fingerprint

        temp = directory / f".{self.MANIFEST_NAME}.{target_fingerprint[:24]}.tmp"
        self.filesystem.write_fsynced(temp, target_bytes)
        self._hit("manifest_temp_fsynced", path=temp)

        self._guard_before_replace(directory, expected_fingerprint)
        self._hit("before_manifest_replace", path=directory / self.MANIFEST_NAME)
        self._guard_before_replace(directory, expected_fingerprint)
        self.filesystem.replace(temp, directory / self.MANIFEST_NAME)
        self._hit("manifest_replaced", path=directory / self.MANIFEST_NAME)
        self.filesystem.fsync_directory(directory)
        self._hit("manifest_directory_fsynced", path=directory)
        return target_fingerprint

    def _guard_before_replace(self, directory: Path, expected: str) -> None:
        current = self.read(str(directory))
        if current.conflicted_copies:
            raise ReconciliationRequired(
                "Nextcloud conflicted copy requires a new scan"
            )
        if current.fingerprint != expected:
            raise ManifestChanged("manifest changed before atomic replacement")

    @staticmethod
    def _assert_reconcilable(
        current: ManifestSnapshot, expected: str, target: str
    ) -> None:
        if current.conflicted_copies:
            raise ReconciliationRequired(
                "Nextcloud conflicted copy requires a new scan"
            )
        if current.fingerprint not in {expected, target}:
            raise ManifestChanged("manifest does not match the planned fingerprint")

    def _hit(self, point: str, *, path: Path) -> None:
        if self._fault is not None:
            self._fault(point, {"path": str(path)})
