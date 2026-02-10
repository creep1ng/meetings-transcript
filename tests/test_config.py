"""Tests for configuration module."""

import os
import tempfile
from pathlib import Path

import pytest


def test_config_loads_from_env_file():
    """Test that configuration loads from .env file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("S3_BUCKET_NAME=test-bucket\n")
        f.write("AWS_REGION=us-west-2\n")
        f.write("MODEL_SIZE=medium\n")
        env_path = f.name

    try:
        from config import load_config

        config = load_config(env_path)
        assert config.s3_bucket_name == "test-bucket"
        assert config.aws_region == "us-west-2"
        assert config.model_size == "medium"
    finally:
        os.unlink(env_path)


def test_config_validates_required_fields():
    """Test that configuration fails with missing required fields."""
    os.environ.clear()

    with pytest.raises(ValueError, match="Missing required environment variables"):
        from config import Config

        Config()


def test_config_validates_model_size():
    """Test that configuration validates model size."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("S3_BUCKET_NAME=test-bucket\n")
        f.write("MODEL_SIZE=invalid\n")
        env_path = f.name

    try:
        from config import load_config

        with pytest.raises(ValueError, match="Invalid MODEL_SIZE"):
            load_config(env_path)
    finally:
        os.unlink(env_path)


def test_config_validates_device():
    """Test that configuration validates device."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("S3_BUCKET_NAME=test-bucket\n")
        f.write("DEVICE=gpu\n")
        env_path = f.name

    try:
        from config import load_config

        with pytest.raises(ValueError, match="DEVICE must be"):
            load_config(env_path)
    finally:
        os.unlink(env_path)


def test_config_defaults():
    """Test that configuration has correct defaults."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("S3_BUCKET_NAME=test-bucket\n")
        env_path = f.name

    try:
        from config import load_config

        config = load_config(env_path)
        assert config.max_retries == 3
        assert config.retry_backoff_base == 2.0
        assert config.s3_timeout == 300
        assert config.transcript_chunk_seconds == 0
    finally:
        os.unlink(env_path)


def test_config_credentials_fallback():
    """Test that missing credentials don't cause error (IAM role fallback)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("S3_BUCKET_NAME=test-bucket\n")
        # Don't set AWS credentials - should use IAM role
        env_path = f.name

    try:
        from config import load_config

        # Should not raise - IAM role will be used
        config = load_config(env_path)
        assert config.aws_access_key_id is None
        assert config.aws_secret_access_key is None
    finally:
        os.unlink(env_path)
