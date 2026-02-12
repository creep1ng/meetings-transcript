#!/usr/bin/env python3
"""
Meetings Transcript - CLI for video transcription using Whisper.

Commands:
    list       - List available videos (S3 or local directory)
    transcribe - Transcribe video(s) from local or S3
    download   - Download transcription from S3
"""

import argparse
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from checkpoint import ShutdownCoordinator
from config import load_config
from s3_client import S3Client, S3Object
from transcribe import TranscriptionResult, TranscriptionService

# Configure logging with secret redaction
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    """Configure logging level with secret redaction."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)

    # Redact sensitive values in logs
    class SensitiveFilter(logging.Filter):
        SENSITIVE_KEYS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}

        def filter(self, record: logging.LogRecord) -> bool:
            for key in self.SENSITIVE_KEYS:
                if hasattr(record, "msg") and key in str(record.msg):
                    record.msg = record.msg.replace(key, "***REDACTED***")
            return True

    logging.getLogger().addFilter(SensitiveFilter())


def get_source(args) -> str:
    """Determine source (local or s3) from args or config.

    Args:
        args: Command line arguments.

    Returns:
        'local' or 's3'.
    """
    # CLI flag takes precedence
    if hasattr(args, "source") and args.source:
        return args.source

    # Load config for default
    try:
        config = load_config(args.env)
        return config.source
    except Exception:
        return "local"


def cmd_list(args) -> None:
    """List available videos from S3 or local directory."""
    source = get_source(args)

    try:
        config = load_config(args.env)
        setup_logging(config.log_level)

        if source == "s3":
            s3_client = S3Client(config)
            videos = s3_client.list_videos()

            if not videos:
                print("No videos found in S3 bucket.")
                return

            print(f"{'Key':<60} {'Size':>10} {'Last Modified':<25}")
            print("-" * 95)

            for video in videos:
                size_mb = video.size_bytes / (1024 * 1024)
                modified = video.last_modified.strftime("%Y-%m-%d %H:%M:%S")
                print(f"{video.key:<60} {size_mb:>7.2f}MB {modified:<25}")

            print(f"\nTotal: {len(videos)} videos in S3")

        else:
            # Local source - list files in path or current directory
            path = Path(".")
            if args.paths:
                path = Path(args.paths[0])

            if not path.exists():
                print(f"Path does not exist: {path}")
                sys.exit(1)

            if path.is_file():
                # Single file
                files = [path]
            else:
                # Directory - list supported files
                exts = config.allowed_video_extensions
                files = sorted(
                    [
                        f
                        for f in path.iterdir()
                        if f.is_file() and f.suffix.lower() in exts
                    ]
                )

            if not files:
                print("No video files found in local directory.")
                return

            print(f"{'File':<60} {'Size':>10}")
            print("-" * 75)

            for f in files:
                size_mb = f.stat().st_size / (1024 * 1024)
                print(f"{str(f):<60} {size_mb:>7.2f}MB")

            print(f"\nTotal: {len(files)} local files")

    except Exception as e:
        logger.error(f"Failed to list videos: {e}")
        sys.exit(1)


def cmd_transcribe(args) -> None:
    """Transcribe video(s) from local or S3."""
    source = get_source(args)

    try:
        config = load_config(args.env)
        setup_logging(config.log_level)

        # Initialize shutdown coordinator if spot drain is enabled
        shutdown_coordinator: Optional[ShutdownCoordinator] = None
        if args.spot_drain or config.spot_drain_enabled:
            poll_interval = (
                args.spot_imds_poll_interval or config.spot_imds_poll_interval
            )
            shutdown_coordinator = ShutdownCoordinator(
                poll_imds=poll_interval > 0,
                poll_interval=poll_interval if poll_interval > 0 else 10.0,
            )
            logger.info(
                "Shutdown coordinator initialized (spot_drain=%s)", args.spot_drain
            )

        transcribe_service = TranscriptionService(config)
        s3_client = S3Client(config) if source == "s3" else None

        s3_files: List[S3Object] = []
        local_files: List[Path] = []

        if source == "s3":
            # S3 source
            if args.all:
                videos = s3_client.list_videos()  # type: ignore
                if not videos:
                    print("No videos found in S3 bucket.")
                    return
                s3_files = videos
            else:
                # Transcribe specific S3 keys
                for key in args.keys:
                    obj = s3_client.list_objects(prefix=key)  # type: ignore
                    s3_files.extend(obj)

        else:
            # Local source
            if args.all:
                # Process all files in current directory or specified path
                path = Path(".")
                if args.paths:
                    path = Path(args.paths[0])

                exts = config.allowed_video_extensions
                local_files = sorted(
                    [
                        f
                        for f in path.iterdir()
                        if f.is_file() and f.suffix.lower() in exts
                    ]
                )
            else:
                # Process specific local paths
                for p in args.paths:
                    path = Path(p)
                    if not path.exists():
                        logger.error(f"Path does not exist: {path}")
                        continue

                    if path.is_file():
                        local_files.append(path)
                    else:
                        # Directory - add all supported files
                        exts = config.allowed_video_extensions
                        local_files.extend(
                            [
                                f
                                for f in path.iterdir()
                                if f.is_file() and f.suffix.lower() in exts
                            ]
                        )

        total_files = len(s3_files) + len(local_files)
        if total_files == 0:
            print("No files to transcribe.")
            return

        logger.info(f"Found {total_files} files to transcribe from {source}")

        interrupted = False

        # Process S3 files
        for file_obj in s3_files:
            if shutdown_coordinator and shutdown_coordinator.should_stop():
                logger.warning("Shutdown requested, stopping S3 file processing")
                interrupted = True
                break

            video_key = file_obj.key
            video_size = file_obj.size_bytes
            logger.info(f"Processing S3: {video_key}")

            # Validate file
            is_valid, error_msg = transcribe_service.validate_file(
                video_key, video_size
            )
            if not is_valid:
                logger.error(f"Validation failed for {video_key}: {error_msg}")
                continue

            # Download from S3 to temp
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                local_video_path = tmp_path / Path(video_key).name

                try:
                    s3_client.download_to_file(video_key, local_video_path)  # type: ignore
                    logger.info(f"Downloaded {video_key}")
                except Exception as e:
                    logger.error(f"Failed to download {video_key}: {e}")
                    continue

                # Transcribe
                output_path = tmp_path / Path(video_key).stem
                result: TranscriptionResult = transcribe_service.process_video(
                    local_video_path,
                    output_path,
                    checkpoint_db=args.checkpoint_db,
                    resume_checkpoint=args.resume,
                    reset_checkpoint=args.reset_checkpoint,
                    checkpoint_sync_s3_uri=args.checkpoint_sync_s3_uri,
                    shutdown_coordinator=shutdown_coordinator,
                )

                if result.success:
                    # Upload to S3
                    s3_key = s3_client.upload_text(  # type: ignore
                        key=video_key,
                        text=result.text,
                        metadata={
                            "source_key": video_key,
                            "source_etag": file_obj.etag or "",
                            "transcribed_at": datetime.now(timezone.utc).isoformat(),
                            "model": config.model_size,
                            "duration_seconds": str(result.duration_seconds or 0),
                        },
                    )
                    logger.info(f"Uploaded transcription to {s3_key}")
                else:
                    logger.error(f"Transcription failed: {result.error}")

        # Process local files
        for local_path in local_files:
            if shutdown_coordinator and shutdown_coordinator.should_stop():
                logger.warning("Shutdown requested, stopping local file processing")
                interrupted = True
                break

            logger.info(f"Processing local: {local_path}")

            # Validate file
            is_valid, error_msg = transcribe_service.validate_file(
                str(local_path), local_path.stat().st_size
            )
            if not is_valid:
                logger.error(f"Validation failed for {local_path}: {error_msg}")
                continue

            # Determine output path
            if args.output:
                output_dir = Path(args.output)
            else:
                output_dir = local_path.parent / "transcripts"

            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / local_path.stem

            # Transcribe
            result: TranscriptionResult = transcribe_service.process_video(
                local_path,
                output_path,
                checkpoint_db=args.checkpoint_db,
                resume_checkpoint=args.resume,
                reset_checkpoint=args.reset_checkpoint,
                checkpoint_sync_s3_uri=args.checkpoint_sync_s3_uri,
                shutdown_coordinator=shutdown_coordinator,
            )

            if result.success:
                logger.info(f"Transcription saved to {result.local_path}")
            else:
                logger.error(f"Transcription failed: {result.error}")

        # Cleanup
        transcribe_service.cleanup()

        if interrupted:
            logger.warning("Transcription interrupted by shutdown request")
            print(f"\nTranscription interrupted - checkpoint saved for resume")
            sys.exit(2)
        else:
            print(f"\nTranscription complete")

    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        sys.exit(1)


def cmd_download(args) -> None:
    """Download transcription from S3."""
    try:
        config = load_config(args.env)
        setup_logging(config.log_level)

        s3_client = S3Client(config)

        for key in args.keys:
            # Try to find the transcription key
            base_name = Path(key).stem
            transcript_key = f"{config.transcripts_prefix}{base_name}.txt"

            if not s3_client.object_exists(transcript_key):
                if s3_client.object_exists(key) and key.endswith(".txt"):
                    transcript_key = key
                else:
                    logger.error(f"Transcription not found: {transcript_key}")
                    continue

            if args.output:
                output_path = Path(args.output) / Path(transcript_key).name
            else:
                output_path = Path.cwd() / Path(transcript_key).name

            s3_client.download_to_file(transcript_key, output_path)
            print(f"Downloaded {transcript_key} to {output_path}")

    except Exception as e:
        logger.error(f"Download failed: {e}")
        sys.exit(1)


def create_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Meetings Transcript - Transcribe videos using Whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (local source):
  %(prog)s list                                    # List local video files
  %(prog)s transcribe video.mp4                     # Transcribe single file
  %(prog)s transcribe --all                         # Transcribe all local files
  %(prog)s transcribe ./folder --all                # Transcribe all in folder

Examples (S3 source):
  %(prog)s list --source s3                         # List S3 videos
  %(prog)s transcribe --source s3 videos/meeting.mp4
  %(prog)s download transcripts/meeting.mp4.txt

Environment:
  SOURCE=local       # Default source (local or s3)
        """,
    )

    parser.add_argument(
        "--env",
        default=".env",
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--source",
        choices=["local", "s3"],
        help="Source type: local files or S3 (overrides SOURCE env var)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # List command
    list_parser = subparsers.add_parser("list", help="List available videos")
    list_parser.add_argument(
        "paths",
        nargs="*",
        help="Path(s) to list (for local source)",
    )
    list_parser.set_defaults(func=cmd_list)

    # Transcribe command
    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe video(s)")
    transcribe_parser.add_argument(
        "paths",
        nargs="*",
        help="File(s) or folder(s) to transcribe",
    )
    transcribe_parser.add_argument(
        "--all",
        action="store_true",
        help="Transcribe all files in source",
    )
    transcribe_parser.add_argument(
        "--transcript-chunk",
        type=int,
        default=0,
        help="Chunk size in seconds for audio transcription (0=disabled)",
    )
    transcribe_parser.add_argument(
        "-o",
        "--output",
        help="Output directory for local transcriptions",
    )
    # Checkpointing flags
    transcribe_parser.add_argument(
        "--checkpoint-db",
        type=str,
        default=None,
        help="Path to checkpoint database (default: .mt_checkpoints/<file>/ckpt.sqlite)",
    )
    transcribe_parser.add_argument(
        "--resume",
        action="store_true",
        dest="resume",
        default=True,
        help="Resume from existing checkpoint (default: True)",
    )
    transcribe_parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not resume from existing checkpoint",
    )
    transcribe_parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        default=False,
        help="Reset checkpoint database before starting",
    )
    transcribe_parser.add_argument(
        "--checkpoint-sync-s3-uri",
        type=str,
        default=None,
        help="S3 URI to sync checkpoint database (not fully implemented in v1)",
    )
    # Spot interruption flags
    transcribe_parser.add_argument(
        "--spot-drain",
        action="store_true",
        default=False,
        help="Enable graceful shutdown on Spot interruption signals",
    )
    transcribe_parser.add_argument(
        "--spot-imds-poll-interval",
        type=float,
        default=0.0,
        help="Interval in seconds for IMDSv2 spot interruption polling (0=disabled)",
    )
    transcribe_parser.set_defaults(func=cmd_transcribe)

    # Download command
    download_parser = subparsers.add_parser(
        "download", help="Download transcription from S3"
    )
    download_parser.add_argument(
        "keys",
        nargs="+",
        help="Transcription keys to download",
    )
    download_parser.add_argument(
        "-o",
        "--output",
        help="Output directory for downloaded files",
    )
    download_parser.set_defaults(func=cmd_download)

    return parser


def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    # Set log level from args if provided
    if hasattr(args, "log_level"):
        setup_logging(args.log_level)

    # Execute command
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
