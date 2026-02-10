#!/usr/bin/env python3
"""
Meetings Transcript - CLI for S3 video transcription using Whisper.

Commands:
    list       - List available videos in S3 bucket
    transcribe - Transcribe video(s) from S3
    download   - Download transcription from S3
"""

import argparse
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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

    # Add filter to root logger
    logging.getLogger().addFilter(SensitiveFilter())


def cmd_list(args) -> None:
    """List available videos in S3 bucket."""
    try:
        config = load_config(args.env)
        setup_logging(config.log_level)

        s3_client = S3Client(config)
        videos = s3_client.list_videos()

        if not videos:
            print("No videos found in S3 bucket.")
            return

        # Print header
        print(f"{'Key':<60} {'Size':>10} {'Last Modified':<25}")
        print("-" * 95)

        for video in videos:
            size_mb = video.size_bytes / (1024 * 1024)
            modified = video.last_modified.strftime("%Y-%m-%d %H:%M:%S")
            print(f"{video.key:<60} {size_mb:>7.2f}MB {modified:<25}")

        print(f"\nTotal: {len(videos)} videos")

    except Exception as e:
        logger.error(f"Failed to list videos: {e}")
        sys.exit(1)


def cmd_transcribe(args) -> None:
    """Transcribe video(s) from S3."""
    try:
        config = load_config(args.env)
        setup_logging(config.log_level)

        s3_client = S3Client(config)
        transcribe_service = TranscriptionService(config)

        # Get list of videos to transcribe
        if args.all:
            videos = s3_client.list_videos()
            if not videos:
                print("No videos found to transcribe.")
                return
            logger.info(f"Found {len(videos)} videos to transcribe")
        else:
            # Transcribe specific video(s)
            videos = []
            for key in args.keys:
                obj = s3_client.list_objects(prefix=key)
                videos.extend(obj)

        # Process each video
        successful = 0
        failed = 0

        for video in videos:
            logger.info(f"Processing: {video.key}")

            # Validate file
            is_valid, error_msg = transcribe_service.validate_file(
                video.key, video.size_bytes
            )
            if not is_valid:
                logger.error(f"Validation failed for {video.key}: {error_msg}")
                failed += 1
                continue

            # Create temp directory for download
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                local_video_path = tmp_path / Path(video.key).name

                # Download from S3
                try:
                    s3_client.download_to_file(video.key, local_video_path)
                    logger.info(f"Downloaded {video.key} to {local_video_path}")
                except Exception as e:
                    logger.error(f"Failed to download {video.key}: {e}")
                    failed += 1
                    continue

                # Transcribe
                output_path = tmp_path / Path(video.key).stem
                result: TranscriptionResult = transcribe_service.process_video(
                    local_video_path, output_path
                )

                if result.success:
                    # Upload to S3
                    s3_key = s3_client.upload_text(
                        key=video.key,
                        text=result.text,
                        metadata={
                            "source_key": video.key,
                            "source_etag": video.etag or "",
                            "transcribed_at": datetime.utcnow().isoformat(),
                            "model": config.model_size,
                            "duration_seconds": str(result.duration_seconds or 0),
                        },
                    )
                    logger.info(f"Uploaded transcription to {s3_key}")
                    successful += 1
                else:
                    logger.error(
                        f"Transcription failed for {video.key}: {result.error}"
                    )
                    failed += 1

        # Cleanup
        transcribe_service.cleanup()

        print(f"\nTranscription complete: {successful} succeeded, {failed} failed")

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
            # Look for .txt files in transcripts prefix
            base_name = Path(key).stem
            transcript_key = f"{config.transcripts_prefix}{base_name}.txt"

            # Try to download
            if not s3_client.object_exists(transcript_key):
                # Try with original key
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
        description="Meetings Transcript - Transcribe videos from S3 using Whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s list                                    # List all videos in S3
  %(prog)s transcribe videos/meeting.mp4           # Transcribe specific video
  %(prog)s transcribe --all                         # Transcribe all videos
  %(prog)s download transcripts/meeting.mp4.txt    # Download transcription
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

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # List command
    list_parser = subparsers.add_parser("list", help="List available videos in S3")
    list_parser.set_defaults(func=cmd_list)

    # Transcribe command
    transcribe_parser = subparsers.add_parser(
        "transcribe", help="Transcribe video(s) from S3"
    )
    transcribe_parser.add_argument(
        "keys",
        nargs="*",
        help="Video keys to transcribe (if not using --all)",
    )
    transcribe_parser.add_argument(
        "--all",
        action="store_true",
        help="Transcribe all videos in the bucket",
    )
    transcribe_parser.add_argument(
        "--transcript-chunk",
        type=int,
        default=0,
        help="Chunk size in seconds for audio transcription (0=disabled)",
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
