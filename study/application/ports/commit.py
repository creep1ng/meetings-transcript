"""Immutable boundaries for manifest reconciliation and asset commits."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from ipaddress import IPv6Address, ip_address
from typing import Protocol
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from study.domain.value_objects import AssetFilename, CourseName

MISSING_MANIFEST_FINGERPRINT = "missing"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_QUERY_RE = re.compile(
    r"(?:^|[_-])(token|secret|password|passwd|credential|api[_-]?key|auth|signature|sig|key[_-]?pair[_-]?id)(?:$|[_-])",
    re.IGNORECASE,
)


class CommitError(RuntimeError):
    """Base error for a commit that did not complete safely."""


class CommitCancelled(CommitError):
    """The plan contains an explicit cancel decision."""


class ManifestChanged(CommitError):
    """The package manifest no longer matches the planned fingerprint."""


class ReconciliationRequired(CommitError):
    """A sync conflict requires a new package scan and explicit reconciliation."""


class UnresolvedConflict(CommitError):
    """A destination collision has no safe explicit resolution."""


class SourceChanged(CommitError):
    """A source differs from the content fingerprint captured by the plan."""


class DestinationChanged(CommitError):
    """A destination changed after conflict planning."""


class IntegrityError(CommitError):
    """Copied content failed size or SHA-256 verification."""


class UnsafePathError(CommitError):
    """A path crosses an adapter safety boundary."""


class ConflictResolution(StrEnum):
    REPLACE = "replace"
    KEEP_BOTH = "keep-both"
    SKIP = "skip"
    CANCEL = "cancel"


class AssetCommitStatus(StrEnum):
    PUBLISHED = "published"
    DEDUPLICATED = "deduplicated"
    SKIPPED = "skipped"


def _validate_id(value: str, field: str = "id") -> None:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field} must be a stable, path-free identifier")


def _validate_component(value: str, field: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"{field} cannot be empty or relative")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must be a single path component")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{field} cannot be an absolute path")


def _collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def normalize_link_url(value: str) -> str:
    """Return the canonical safe representation accepted by link manifests."""

    stripped = value.strip()
    if any(ord(character) < 32 for character in stripped):
        raise ValueError("link URL cannot contain control characters")
    parsed = urlsplit(stripped)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("link URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("link URL cannot contain credentials")

    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(_TOKEN_QUERY_RE.search(key) for key, _ in query):
        raise ValueError("link URL cannot contain credential-like query parameters")

    raw_host = parsed.hostname
    try:
        address = ip_address(raw_host)
        host = address.compressed
    except ValueError:
        host = raw_host.encode("idna").decode("ascii").lower()
        address = None
    if isinstance(address, IPv6Address):
        host = f"[{host}]"
    port = parsed.port
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=")
    normalized_query = urlencode(sorted(query), doseq=True)
    return urlunsplit((parsed.scheme.lower(), host, path, normalized_query, ""))


@dataclass(frozen=True, slots=True)
class FileAsset:
    id: str
    filename: str
    kind: str
    media_type: str
    size: int
    sha256: str
    original_filename: str
    status: str
    derived_from: str | None = None
    type: str = "file"

    def __post_init__(self) -> None:
        _validate_id(self.id)
        try:
            filename = AssetFilename(self.filename).value
            original_filename = AssetFilename(self.original_filename).value
        except ValueError as error:
            raise ValueError(
                f"filename must be a safe path component: {error}"
            ) from error
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "original_filename", original_filename)
        if self.type != "file":
            raise ValueError("file asset type must be 'file'")
        if not self.kind or not self.media_type or not self.status:
            raise ValueError("kind, media_type, and status are required")
        if isinstance(self.size, bool) or not isinstance(self.size, int):
            raise TypeError("size must be an integer")
        if self.size < 0:
            raise ValueError("size cannot be negative")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if self.derived_from is not None:
            _validate_id(self.derived_from, "derived_from")


@dataclass(frozen=True, slots=True)
class LinkAsset:
    id: str
    url: str
    label: str
    type: str = "link"

    def __post_init__(self) -> None:
        _validate_id(self.id)
        if self.type != "link":
            raise ValueError("link asset type must be 'link'")
        if not self.label.strip():
            raise ValueError("link label is required")
        if any(unicodedata.category(character) == "Cc" for character in self.label):
            raise ValueError("link label cannot contain control characters")
        if self.url != normalize_link_url(self.url):
            raise ValueError("link URL must be normalized before creating the asset")


type ManifestAsset = FileAsset | LinkAsset


@dataclass(frozen=True, slots=True)
class ClassPackageManifest:
    course: str
    class_date: str
    updated_at: str
    assets: tuple[ManifestAsset, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("only class package manifest v1 is supported")
        object.__setattr__(self, "course", CourseName(self.course).value)
        if date.fromisoformat(self.class_date).isoformat() != self.class_date:
            raise ValueError("class_date must be an ISO date")
        timestamp = datetime.fromisoformat(self.updated_at)
        if not self.updated_at.endswith("Z") or timestamp.tzinfo != UTC:
            raise ValueError("updated_at must be a UTC timestamp ending in Z")
        ids: set[str] = set()
        filenames: set[str] = set()
        for asset in self.assets:
            if asset.id in ids:
                raise ValueError(f"duplicate asset id: {asset.id}")
            ids.add(asset.id)
            if isinstance(asset, FileAsset):
                key = _collision_key(asset.filename)
                if key in filenames:
                    raise ValueError(f"colliding manifest filename: {asset.filename}")
                filenames.add(key)


@dataclass(frozen=True, slots=True)
class ManifestSnapshot:
    manifest: ClassPackageManifest | None
    fingerprint: str
    conflicted_copies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedFile:
    asset: FileAsset
    source_path: str
    conflict: ConflictResolution | None = None
    expected_destination_sha256: str | None = None
    delete_source: bool = True

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("source_path is required")
        if self.expected_destination_sha256 is not None and not _SHA256_RE.fullmatch(
            self.expected_destination_sha256
        ):
            raise ValueError("expected destination hash must be SHA-256")


@dataclass(frozen=True, slots=True)
class CommitAssetsCommand:
    package_dir: str
    course: str
    class_date: str
    updated_at: str
    expected_manifest_fingerprint: str
    base_manifest: ClassPackageManifest | None = None
    discovered_assets: tuple[FileAsset, ...] = ()
    files: tuple[PlannedFile, ...] = ()
    links: tuple[LinkAsset, ...] = ()


@dataclass(frozen=True, slots=True)
class AssetCommitOutcome:
    asset_id: str
    filename: str
    status: AssetCommitStatus


@dataclass(frozen=True, slots=True)
class CommitAssetsResult:
    manifest_fingerprint: str
    outcomes: tuple[AssetCommitOutcome, ...]
    deleted_sources: tuple[str, ...] = ()
    retained_sources: tuple[str, ...] = ()


class ManifestStore(Protocol):
    def read(self, package_dir: str) -> ManifestSnapshot: ...

    def fingerprint(self, manifest: ClassPackageManifest) -> str: ...

    def commit(
        self,
        package_dir: str,
        manifest: ClassPackageManifest,
        expected_fingerprint: str,
    ) -> str: ...


class AssetCommitter(Protocol):
    def commit(
        self,
        command: CommitAssetsCommand,
        manifest: ClassPackageManifest,
        files: tuple[PlannedFile, ...],
    ) -> CommitAssetsResult: ...
