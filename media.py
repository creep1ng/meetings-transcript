"""Source-neutral media discovery and acquisition metadata."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from config import Config
from s3_client import S3Client, S3Object


@dataclass(frozen=True)
class MediaItem:
    """A media input independent of its backing storage."""

    source_id: str
    original_filename: str
    size_bytes: int
    source_type: str
    source_modified_at: datetime
    local_path: Optional[Path] = None
    etag: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        modified = self.source_modified_at.astimezone(timezone.utc).isoformat()
        return f"{self.source_type}:{self.source_id}:{self.size_bytes}:{modified}:{self.etag or ''}"


def discover_local_media(
    paths: Iterable[str], all_files: bool, config: Config
) -> list[MediaItem]:
    """Discover supported local media without duplicating extension filtering."""
    requested = [Path(value) for value in paths]
    if all_files and not requested:
        requested = [Path(".")]

    files: list[Path] = []
    for path in requested:
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        if path.is_file():
            if path.suffix.lower() in config.allowed_media_extensions:
                files.append(path)
        else:
            files.extend(
                child
                for child in path.iterdir()
                if child.is_file()
                and child.suffix.lower() in config.allowed_media_extensions
            )

    items: list[MediaItem] = []
    seen: set[Path] = set()
    for path in sorted(files):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        stat = resolved.stat()
        items.append(
            MediaItem(
                source_id=str(resolved),
                original_filename=resolved.name,
                size_bytes=stat.st_size,
                source_type="local",
                source_modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                local_path=resolved,
            )
        )
    return items


def discover_s3_media(
    keys: Iterable[str], all_files: bool, client: S3Client, config: Config
) -> list[MediaItem]:
    """Discover supported S3 media and materialize the listing once."""
    objects: list[S3Object] = []
    if all_files:
        objects = client.list_media()
    else:
        for key in keys:
            objects.extend(
                client.list_objects(
                    prefix=key, extensions=config.allowed_media_extensions
                )
            )

    return [
        MediaItem(
            source_id=obj.key,
            original_filename=Path(obj.key).name,
            size_bytes=obj.size_bytes,
            source_type="s3",
            source_modified_at=_aware(obj.last_modified),
            etag=obj.etag,
        )
        for obj in objects
    ]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
