#!/usr/bin/env python3
"""CLI for transcribing local or S3 media to local, S3, or WebDAV storage."""

import argparse
import hashlib
import logging
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from checkpoint import ShutdownCoordinator
from config import Config, load_config
from folder_selector import select_webdav_folder
from markdown_output import format_obsidian_markdown, probe_recorded_at
from media import MediaItem, discover_local_media, discover_s3_media
from s3_client import S3Client
from transcribe import TranscriptionResult, TranscriptionService
from webdav_client import WebDAVClient, WebDAVResource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class SensitiveFilter(logging.Filter):
    """Redact credential names and configured secret values."""

    KEY_NAMES = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "WEBDAV_USERNAME",
        "WEBDAV_PASSWORD",
        "HF_TOKEN",
    )

    def __init__(self, values: tuple[str, ...] = ()):
        super().__init__()
        self.values = tuple(value for value in values if value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for sensitive in self.KEY_NAMES + self.values:
            message = message.replace(sensitive, "***REDACTED***")
        record.msg = message
        record.args = ()
        return True


@dataclass(frozen=True)
class WebDAVTarget:
    path: str
    overwrite: bool
    preflight_exists: bool
    expected_etag: Optional[str] = None
    expected_modified_at: Optional[datetime] = None


def setup_logging(level: str, config: Optional[Config] = None) -> None:
    """Configure logging and replace prior secret filters."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.filters = [
        entry for entry in root.filters if not isinstance(entry, SensitiveFilter)
    ]
    values: tuple[str, ...] = ()
    if config:
        values = tuple(
            value
            for value in (
                config.aws_access_key_id,
                config.aws_secret_access_key,
                config.webdav_username,
                config.webdav_password,
            )
            if value
        )
    sensitive_filter = SensitiveFilter(values)
    root.addFilter(sensitive_filter)
    for handler in root.handlers:
        handler.filters = [
            entry for entry in handler.filters if not isinstance(entry, SensitiveFilter)
        ]
        handler.addFilter(sensitive_filter)


def get_source(args: argparse.Namespace) -> str:
    """Resolve source with CLI precedence and a local fallback."""
    try:
        return load_config(args.env, source=getattr(args, "source", None)).source
    except Exception:
        return getattr(args, "source", None) or "local"


def _load_runtime_config(args: argparse.Namespace) -> Config:
    config = load_config(
        args.env,
        source=getattr(args, "source", None),
        destination=getattr(args, "destination", None),
    )
    if getattr(args, "transcript_chunk", None) is not None:
        config.transcript_chunk_seconds = args.transcript_chunk
    setup_logging(getattr(args, "log_level", config.log_level), config)
    return config


def cmd_list(args: argparse.Namespace) -> int:
    """List available media from S3 or local storage."""
    try:
        config = _load_runtime_config(args)
        if config.source == "s3":
            items = discover_s3_media([], True, S3Client(config), config)
        else:
            items = discover_local_media(args.paths or ["."], True, config)

        if not items:
            print(f"No media files found in {config.source} source.")
            return 0
        print(f"{'Media':<60} {'Size':>10} {'Last Modified':<25}")
        print("-" * 97)
        for item in items:
            size_mb = item.size_bytes / (1024 * 1024)
            modified = item.source_modified_at.strftime("%Y-%m-%d %H:%M:%S")
            print(f"{item.source_id:<60} {size_mb:>7.2f}MB {modified:<25}")
        print(f"\nTotal: {len(items)} media files")
        return 0
    except Exception as exc:
        logger.error("Failed to list media: %s", exc)
        return 1


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Transcribe a batch and publish each successful result."""
    service: Optional[TranscriptionService] = None
    try:
        config = _load_runtime_config(args)
        destination = config.effective_destination
        s3_client = S3Client(config) if "s3" in (config.source, destination) else None
        items = _discover_items(args, config, s3_client)
        if not items:
            print("No media files to transcribe.")
            return 0

        webdav_client: Optional[WebDAVClient] = None
        webdav_targets: dict[str, WebDAVTarget] = {}
        skipped = 0
        if destination == "webdav":
            webdav_client = WebDAVClient(config)
            prepared = _prepare_webdav_targets(items, webdav_client, config)
            if prepared is None:
                print("WebDAV publication cancelled; no files were transcribed.")
                return 1
            webdav_targets, skipped = prepared
            items = [item for item in items if item.source_id in webdav_targets]
            if not items:
                print(f"Nothing to transcribe ({skipped} skipped).")
                return 0

        shutdown = _create_shutdown_coordinator(args, config)
        service = TranscriptionService(config)
        uploaded = 0
        failed = 0
        interrupted = False

        for item in items:
            if shutdown and shutdown.should_stop():
                interrupted = True
                break
            valid, error = service.validate_file(item.source_id, item.size_bytes)
            if not valid:
                logger.error("Validation failed for %s: %s", item.source_id, error)
                failed += 1
                continue

            try:
                with _acquire_media(item, s3_client) as local_path:
                    recorded_at = probe_recorded_at(local_path)
                    output_base = _output_base(item, args, destination)
                    result = service.process_media(
                        local_path,
                        output_base,
                        checkpoint_db=args.checkpoint_db,
                        resume_checkpoint=args.resume,
                        reset_checkpoint=args.reset_checkpoint,
                        checkpoint_sync_s3_uri=args.checkpoint_sync_s3_uri,
                        shutdown_coordinator=shutdown,
                        source_uri=item.source_id,
                        source_fingerprint=item.fingerprint,
                    )
                    if not result.success:
                        logger.error(
                            "Transcription failed for %s: %s",
                            item.source_id,
                            result.error,
                        )
                        failed += 1
                        continue
                    _publish_result(
                        item,
                        result,
                        recorded_at,
                        destination,
                        config,
                        s3_client,
                        webdav_client,
                        webdav_targets.get(item.source_id),
                    )
                    uploaded += 1
            except Exception as exc:
                logger.error("Failed to process %s: %s", item.source_id, exc)
                failed += 1

        if interrupted:
            print("Transcription interrupted; checkpoint saved for resume.")
            return 2
        print(
            f"Transcription summary: published={uploaded}, skipped={skipped}, failed={failed}"
        )
        return 1 if failed else 0
    except Exception as exc:
        logger.error("Transcription failed: %s", exc)
        return 1
    finally:
        if service is not None:
            service.cleanup()


def _discover_items(
    args: argparse.Namespace, config: Config, s3_client: Optional[S3Client]
) -> list[MediaItem]:
    if config.source == "s3":
        assert s3_client is not None
        return discover_s3_media(args.paths, args.all, s3_client, config)
    return discover_local_media(args.paths, args.all, config)


def _create_shutdown_coordinator(
    args: argparse.Namespace, config: Config
) -> Optional[ShutdownCoordinator]:
    if not (args.spot_drain or config.spot_drain_enabled):
        return None
    interval = args.spot_imds_poll_interval or config.spot_imds_poll_interval
    return ShutdownCoordinator(
        poll_imds=interval > 0,
        poll_interval=interval if interval > 0 else 10.0,
    )


def _prepare_webdav_targets(
    items: list[MediaItem], client: WebDAVClient, config: Config
) -> Optional[tuple[dict[str, WebDAVTarget], int]]:
    selected = select_webdav_folder(
        client,
        config.webdav_media_subfolder,
        tuple(item.original_filename for item in items),
    )
    if selected is None:
        return None
    media_path = str(PurePosixPath(selected) / config.webdav_media_subfolder)
    client.create_collection(media_path)

    targets: dict[str, WebDAVTarget] = {}
    skipped = 0
    reserved: set[str] = set()
    for item in items:
        target = str(PurePosixPath(media_path) / f"{item.original_filename}.md")
        existing = client.get_metadata(target)
        if existing is None and target not in reserved:
            targets[item.source_id] = WebDAVTarget(target, False, False)
            reserved.add(target)
            continue
        decision = _prompt_conflict(item, target, existing)
        if decision == "cancel":
            return None
        if decision == "skip":
            skipped += 1
            continue
        targets[item.source_id] = WebDAVTarget(
            target,
            True,
            existing is not None,
            existing.etag if existing else None,
            existing.modified_at if existing else None,
        )
        reserved.add(target)
    return targets, skipped


def _prompt_conflict(
    item: MediaItem, target: str, existing: Optional[WebDAVResource]
) -> str:
    modified = (
        existing.modified_at.astimezone(timezone.utc).isoformat()
        if existing and existing.modified_at
        else "unknown"
    )
    while True:
        choice = (
            input(
                f"Conflict for {item.original_filename} at {target} "
                f"(remote modified: {modified}). [r]eplace/[s]kip/[c]ancel: "
            )
            .strip()
            .lower()
        )
        if choice in ("r", "replace"):
            return "replace"
        if choice in ("s", "skip"):
            return "skip"
        if choice in ("c", "cancel"):
            return "cancel"


@contextmanager
def _acquire_media(item: MediaItem, s3_client: Optional[S3Client]) -> Iterator[Path]:
    if item.local_path is not None:
        yield item.local_path
        return
    if s3_client is None:
        raise RuntimeError("S3 client is required to acquire this media item.")
    with tempfile.TemporaryDirectory() as temporary:
        local_path = Path(temporary) / item.original_filename
        s3_client.download_to_file(item.source_id, local_path)
        yield local_path


def _output_base(item: MediaItem, args: argparse.Namespace, destination: str) -> Path:
    if destination == "local":
        if args.output:
            directory = Path(args.output)
        elif item.local_path:
            directory = item.local_path.parent / "transcripts"
        else:
            directory = Path.cwd() / "transcripts"
    else:
        digest = hashlib.sha256(item.source_id.encode()).hexdigest()[:12]
        directory = Path.cwd() / "transcripts" / ".staging" / digest
    directory.mkdir(parents=True, exist_ok=True)
    return directory / Path(item.original_filename).stem


def _publish_result(
    item: MediaItem,
    result: TranscriptionResult,
    recorded_at: Optional[datetime],
    destination: str,
    config: Config,
    s3_client: Optional[S3Client],
    webdav_client: Optional[WebDAVClient],
    webdav_target: Optional[WebDAVTarget],
) -> None:
    if destination == "local":
        logger.info("Transcription saved to %s", result.local_path)
        return
    if destination == "s3":
        assert s3_client is not None
        key = s3_client.upload_text(
            key=item.source_id,
            text=result.text,
            metadata={
                "source_key": item.source_id,
                "source_etag": item.etag or "",
                "transcribed_at": datetime.now(timezone.utc).isoformat(),
                "model": config.model_size,
                "duration_seconds": str(result.duration_seconds or 0),
            },
        )
        logger.info("Uploaded transcription to %s", key)
        return

    assert webdav_client is not None and webdav_target is not None
    markdown = format_obsidian_markdown(
        item,
        result,
        config.model_size,
        datetime.now(timezone.utc),
        recorded_at,
    )
    staging = (result.local_path or Path(result.key)).with_name(
        f"{item.original_filename}.md"
    )
    staging.write_text(markdown, encoding="utf-8")
    try:
        webdav_client.upload_atomic(
            webdav_target.path,
            markdown.encode("utf-8"),
            webdav_target.overwrite,
            preflight_exists=webdav_target.preflight_exists,
            expected_etag=webdav_target.expected_etag,
            expected_modified_at=webdav_target.expected_modified_at,
        )
    except Exception:
        recovery = Path.cwd() / "transcripts" / "webdav-failed"
        recovery.mkdir(parents=True, exist_ok=True)
        recovered = recovery / staging.name
        if recovered.exists():
            recovered = (
                recovery / f"{staging.stem}-{uuid.uuid4().hex[:8]}{staging.suffix}"
            )
        shutil.move(str(staging), recovered)
        logger.error(
            "WebDAV upload failed; recoverable Markdown retained at %s", recovered
        )
        raise
    staging.unlink(missing_ok=True)
    logger.info("Published Markdown to WebDAV path %s", webdav_target.path)


def cmd_download(args: argparse.Namespace) -> int:
    """Download existing text transcripts from S3."""
    try:
        config = load_config(args.env, source="s3", destination="s3")
        setup_logging(args.log_level, config)
        client = S3Client(config)
        failed = 0
        for key in args.keys:
            transcript_key = f"{config.transcripts_prefix}{Path(key).stem}.txt"
            if not client.object_exists(transcript_key):
                if client.object_exists(key) and key.endswith(".txt"):
                    transcript_key = key
                else:
                    logger.error("Transcription not found: %s", transcript_key)
                    failed += 1
                    continue
            output = Path(args.output) if args.output else Path.cwd()
            output.mkdir(parents=True, exist_ok=True)
            destination = output / Path(transcript_key).name
            client.download_to_file(transcript_key, destination)
            print(f"Downloaded {transcript_key} to {destination}")
        return 1 if failed else 0
    except Exception as exc:
        logger.error("Download failed: %s", exc)
        return 1


def cmd_study(args: argparse.Namespace) -> int:
    """Launch the academic organizer without importing it for legacy commands."""

    try:
        from study.bootstrap import run_study

        return run_study(args.env)
    except Exception as exc:
        logger.error("Study organizer failed: %s", exc)
        return 1


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe local or S3 media using Whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s list ./recordings
  %(prog)s transcribe meeting.mp3
  %(prog)s --source s3 --destination webdav transcribe --all

Precedence: CLI flags, environment values, then legacy source-derived destination.
""",
    )
    parser.add_argument("--env", default=".env", help="Path to environment file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument("--source", choices=["local", "s3"])
    parser.add_argument("--destination", choices=["local", "s3", "webdav"])

    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser("list", help="List available media")
    list_parser.add_argument("paths", nargs="*", help="Local path(s) to list")
    list_parser.set_defaults(func=cmd_list)

    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe media")
    transcribe_parser.add_argument(
        "paths", nargs="*", help="Media files, folders, or S3 keys"
    )
    transcribe_parser.add_argument(
        "--all", action="store_true", help="Transcribe all media in source"
    )
    transcribe_parser.add_argument(
        "--transcript-chunk",
        type=int,
        default=None,
        help="Chunk size in seconds (0 disables chunking)",
    )
    transcribe_parser.add_argument("-o", "--output", help="Local output directory")
    transcribe_parser.add_argument("--checkpoint-db")
    transcribe_parser.add_argument("--resume", action="store_true", default=True)
    transcribe_parser.add_argument("--no-resume", action="store_false", dest="resume")
    transcribe_parser.add_argument("--reset-checkpoint", action="store_true")
    transcribe_parser.add_argument("--checkpoint-sync-s3-uri")
    transcribe_parser.add_argument("--spot-drain", action="store_true")
    transcribe_parser.add_argument("--spot-imds-poll-interval", type=float, default=0.0)
    transcribe_parser.set_defaults(func=cmd_transcribe)

    download_parser = subparsers.add_parser(
        "download", help="Download a transcript from S3"
    )
    download_parser.add_argument("keys", nargs="+")
    download_parser.add_argument("-o", "--output")
    download_parser.set_defaults(func=cmd_download)

    study_parser = subparsers.add_parser(
        "study", help="Organize and transcribe local class materials"
    )
    study_parser.set_defaults(func=cmd_study)
    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 1
    setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
