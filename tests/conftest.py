"""Test configuration and fixtures."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    from config import Config
    from s3_client import S3Client

    config = Config(source="s3", s3_bucket_name="test-bucket")
    client = S3Client.__new__(S3Client)
    client.config = config
    client._client = MagicMock()
    client._resource = MagicMock()

    return client


@pytest.fixture
def sample_s3_object():
    """Create a sample S3 object for testing."""
    from datetime import datetime

    from s3_client import S3Object

    return S3Object(
        key="videos/test_meeting.mp4",
        size_bytes=1024 * 1024 * 100,  # 100MB
        last_modified=datetime.now(),
        etag='"abc123"',
    )


@pytest.fixture
def sample_video_file(temp_dir):
    """Create a sample video file for testing."""
    video_path = temp_dir / "test_video.mp4"
    # Create a small file to simulate video
    video_path.write_bytes(b"fake video content" * 1000)
    return video_path
