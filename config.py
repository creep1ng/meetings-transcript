"""Configuration loader and validator for environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """Configuration class with validation and defaults."""

    # AWS credentials (required for S3 access)
    aws_access_key_id: Optional[str] = field(default=None)
    aws_secret_access_key: Optional[str] = field(default=None)
    aws_region: str = field(default="us-east-1")

    # Source configuration
    source: str = field(default="local")  # local or s3

    # S3 bucket configuration (required for S3 source)
    s3_bucket_name: Optional[str] = field(default=None)
    video_prefix: str = field(default="videos/")
    transcripts_prefix: str = field(default="transcripts/")

    # Transcription settings
    transcript_provider: str = field(default="whisper/local")
    model_size: str = field(default="small")
    device: str = field(default="cpu")
    transcript_chunk_seconds: int = field(default=0)
    download_chunk_bytes: int = field(default=1024 * 1024 * 10)  # 10MB default

    # Checkpointing and Spot handling
    checkpoint_db: Optional[str] = field(default=None)
    resume_checkpoint: bool = field(default=True)
    reset_checkpoint: bool = field(default=False)
    checkpoint_sync_s3_uri: Optional[str] = field(default=None)
    spot_drain_enabled: bool = field(default=False)
    spot_imds_poll_interval: float = field(default=0.0)

    # Concurrency
    max_concurrent_jobs: int = field(default=1)

    # Timeouts and retries (seconds)
    s3_timeout: int = field(default=300)
    transcription_timeout: int = field(default=3600)
    max_retries: int = field(default=3)
    retry_backoff_base: float = field(default=2.0)

    # Validation limits
    max_file_size_bytes: int = field(default=1024 * 1024 * 1024 * 2)  # 2GB default
    allowed_video_extensions: tuple = field(
        default_factory=lambda: (".mp4", ".mov", ".mkv", ".wav")
    )

    # Logging and tracing
    log_level: str = field(default="INFO")
    enable_tracing: bool = field(default=False)

    def __post_init__(self):
        """Validate configuration after initialization."""
        # First apply environment overrides (which loads .env file values)
        self._apply_env_overrides()
        # Then validate required variables
        self._validate_required()
        # Finally validate coherence
        self._validate_coherence()

    def _validate_required(self):
        """Check that required environment variables are set."""
        missing = []

        # Check AWS credentials - either both keys or neither (role will be used)
        has_access_key = self.aws_access_key_id is not None
        has_secret_key = self.aws_secret_access_key is not None

        if has_access_key != has_secret_key:
            raise ValueError(
                "Both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set together, "
                "or neither (to use IAM role/SSO)."
            )

        if self.s3_bucket_name is None:
            missing.append("S3_BUCKET_NAME")

        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Please set them in your .env file or environment."
            )

    def _apply_env_overrides(self):
        """Apply environment variable overrides to configuration."""
        # AWS credentials
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            self.aws_access_key_id = os.environ["AWS_ACCESS_KEY_ID"]
        if os.environ.get("AWS_SECRET_ACCESS_KEY"):
            self.aws_secret_access_key = os.environ["AWS_SECRET_ACCESS_KEY"]
        if os.environ.get("AWS_REGION"):
            self.aws_region = os.environ["AWS_REGION"]

        # Source configuration
        if os.environ.get("SOURCE"):
            self.source = os.environ["SOURCE"]

        # S3 configuration
        if os.environ.get("S3_BUCKET_NAME"):
            self.s3_bucket_name = os.environ["S3_BUCKET_NAME"]
        if os.environ.get("VIDEO_PREFIX"):
            self.video_prefix = os.environ["VIDEO_PREFIX"]
        if os.environ.get("TRANSCRIPTS_PREFIX"):
            self.transcripts_prefix = os.environ["TRANSCRIPTS_PREFIX"]

        # Transcription settings
        if os.environ.get("TRANSCRIBE_PROVIDER"):
            self.transcript_provider = os.environ["TRANSCRIBE_PROVIDER"]
        if os.environ.get("MODEL_SIZE"):
            self.model_size = os.environ["MODEL_SIZE"]
        if os.environ.get("DEVICE"):
            self.device = os.environ["DEVICE"]
        if os.environ.get("TRANSCRIPT_CHUNK_SECONDS"):
            self.transcript_chunk_seconds = int(os.environ["TRANSCRIPT_CHUNK_SECONDS"])
        if os.environ.get("DOWNLOAD_CHUNK_BYTES"):
            self.download_chunk_bytes = int(os.environ["DOWNLOAD_CHUNK_BYTES"])

        # Checkpointing and Spot
        if os.environ.get("CHECKPOINT_DB"):
            self.checkpoint_db = os.environ["CHECKPOINT_DB"]
        if os.environ.get("RESUME_CHECKPOINT"):
            self.resume_checkpoint = os.environ["RESUME_CHECKPOINT"].lower() in (
                "true",
                "1",
                "yes",
            )
        if os.environ.get("RESET_CHECKPOINT"):
            self.reset_checkpoint = os.environ["RESET_CHECKPOINT"].lower() in (
                "true",
                "1",
                "yes",
            )
        if os.environ.get("CHECKPOINT_SYNC_S3_URI"):
            self.checkpoint_sync_s3_uri = os.environ["CHECKPOINT_SYNC_S3_URI"]
        if os.environ.get("SPOT_DRAIN_ENABLED"):
            self.spot_drain_enabled = os.environ["SPOT_DRAIN_ENABLED"].lower() in (
                "true",
                "1",
                "yes",
            )
        if os.environ.get("SPOT_IMDS_POLL_INTERVAL"):
            self.spot_imds_poll_interval = float(os.environ["SPOT_IMDS_POLL_INTERVAL"])

        # Concurrency
        if os.environ.get("MAX_CONCURRENT_JOBS"):
            self.max_concurrent_jobs = int(os.environ["MAX_CONCURRENT_JOBS"])

        # Timeouts and retries
        if os.environ.get("S3_TIMEOUT"):
            self.s3_timeout = int(os.environ["S3_TIMEOUT"])
        if os.environ.get("TRANSCRIPTION_TIMEOUT"):
            self.transcription_timeout = int(os.environ["TRANSCRIPTION_TIMEOUT"])
        if os.environ.get("MAX_RETRIES"):
            self.max_retries = int(os.environ["MAX_RETRIES"])
        if os.environ.get("RETRY_BACKOFF_BASE"):
            self.retry_backoff_base = float(os.environ["RETRY_BACKOFF_BASE"])

        # Validation limits
        if os.environ.get("MAX_FILE_SIZE_BYTES"):
            self.max_file_size_bytes = int(os.environ["MAX_FILE_SIZE_BYTES"])

        # Logging and tracing
        if os.environ.get("LOG_LEVEL"):
            self.log_level = os.environ["LOG_LEVEL"]
        if os.environ.get("ENABLE_TRACING"):
            self.enable_tracing = os.environ["ENABLE_TRACING"].lower() in (
                "true",
                "1",
                "yes",
            )

    def _validate_coherence(self):
        """Validate that configuration values are coherent."""
        if self.max_concurrent_jobs < 1:
            raise ValueError("MAX_CONCURRENT_JOBS must be >= 1")

        if self.max_retries < 0:
            raise ValueError("MAX_RETRIES must be >= 0")

        if self.retry_backoff_base < 1.0:
            raise ValueError("RETRY_BACKOFF_BASE must be >= 1.0")

        if self.download_chunk_bytes < 1024:
            raise ValueError("DOWNLOAD_CHUNK_BYTES must be >= 1024")

        if self.transcript_chunk_seconds < 0:
            raise ValueError("TRANSCRIPT_CHUNK_SECONDS must be >= 0")

        if self.spot_imds_poll_interval < 0:
            raise ValueError("SPOT_IMDS_POLL_INTERVAL must be >= 0")

        if self.model_size not in ("tiny", "base", "small", "medium", "large", "turbo"):
            raise ValueError(
                f"Invalid MODEL_SIZE: {self.model_size}. "
                "Must be one of: tiny, base, small, medium, large, turbo"
            )

        if self.device not in ("cpu", "cuda"):
            raise ValueError("DEVICE must be 'cpu' or 'cuda'")

        # Validate source is valid
        if self.source not in ("local", "s3"):
            raise ValueError(f"Invalid SOURCE: {self.source}. Must be 'local' or 's3'.")

        # Validate S3 bucket is required for S3 source
        if self.source == "s3" and self.s3_bucket_name is None:
            raise ValueError("S3_BUCKET_NAME is required when SOURCE='s3'.")

        # Validate provider is supported
        if self.transcript_provider != "whisper/local":
            raise ValueError(
                f"Unsupported TRANSCRIBE_PROVIDER: {self.transcript_provider}. "
                "Only 'whisper/local' is currently supported."
            )


def load_config(env_path: str = ".env") -> Config:
    """Load configuration from environment variables and .env file.

    Args:
        env_path: Path to .env file (optional).

    Returns:
        Config object with validated configuration.

    Raises:
        ValueError: If required variables are missing or values are invalid.
    """
    # Load .env file if it exists and temporarily apply overrides
    overrides: dict[str, str] = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                overrides[key.strip()] = val.strip()

    previous: dict[str, Optional[str]] = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value

    try:
        return Config()
    finally:
        # Restore environment so tests and other calls remain isolated
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
