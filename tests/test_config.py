"""Tests for source/destination-aware configuration."""

import pytest

from config import Config, load_config


def test_local_defaults_do_not_require_s3(monkeypatch):
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.delenv("SOURCE", raising=False)
    monkeypatch.delenv("DESTINATION", raising=False)
    config = Config()
    assert config.source == "local"
    assert config.effective_destination == "local"
    assert ".mp3" in config.allowed_media_extensions


@pytest.mark.parametrize(
    ("source", "destination"),
    [("s3", None), ("local", "s3")],
)
def test_s3_bucket_required_only_when_s3_is_used(monkeypatch, source, destination):
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.delenv("SOURCE", raising=False)
    monkeypatch.delenv("DESTINATION", raising=False)
    with pytest.raises(ValueError, match="S3_BUCKET_NAME"):
        Config(source=source, destination=destination)


def test_webdav_requires_credentials(monkeypatch):
    for key in ("WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD", "DESTINATION"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="WEBDAV_URL"):
        Config(destination="webdav")


def test_webdav_normalizes_safe_root(monkeypatch):
    monkeypatch.delenv("DESTINATION", raising=False)
    config = Config(
        destination="webdav",
        webdav_url="https://cloud.example.com/remote.php/dav/files/alice",
        webdav_username="alice",
        webdav_password="app-password",
        webdav_root_path="/Notes/Vault/",
    )
    assert config.webdav_root_path == "/Notes/Vault"


@pytest.mark.parametrize("root", ["../Vault", "/Vault/../Other"])
def test_webdav_rejects_root_traversal(monkeypatch, root):
    monkeypatch.delenv("DESTINATION", raising=False)
    with pytest.raises(ValueError, match="cannot contain"):
        Config(webdav_root_path=root)


@pytest.mark.parametrize("subfolder", ["", ".", "..", "Media/Other", "Media\\Other"])
def test_webdav_rejects_unsafe_media_segment(monkeypatch, subfolder):
    monkeypatch.delenv("WEBDAV_MEDIA_SUBFOLDER", raising=False)
    with pytest.raises(ValueError, match="one safe path segment"):
        Config(webdav_media_subfolder=subfolder)


def test_cli_overrides_environment_destination(tmp_path, monkeypatch):
    env_file = tmp_path / "settings.env"
    env_file.write_text("SOURCE=s3\nDESTINATION=s3\nS3_BUCKET_NAME=bucket\n")
    config = load_config(str(env_file), source="local", destination="local")
    assert config.source == "local"
    assert config.effective_destination == "local"


def test_config_validates_model_and_device(monkeypatch):
    monkeypatch.delenv("MODEL_SIZE", raising=False)
    monkeypatch.delenv("DEVICE", raising=False)
    with pytest.raises(ValueError, match="Invalid MODEL_SIZE"):
        Config(model_size="invalid")
    with pytest.raises(ValueError, match="DEVICE must be"):
        Config(device="gpu")
