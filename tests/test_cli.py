import sys
from argparse import Namespace
from datetime import datetime, timezone
from types import ModuleType

import pytest

import main
from config import Config
from media import MediaItem
from transcribe import TranscriptionResult


def args_for(path):
    return Namespace(
        env="missing.env",
        source="local",
        destination="webdav",
        log_level="INFO",
        transcript_chunk=None,
        paths=[str(path)],
        all=False,
        output=None,
        checkpoint_db=None,
        resume=True,
        reset_checkpoint=False,
        checkpoint_sync_s3_uri=None,
        spot_drain=False,
        spot_imds_poll_interval=0.0,
    )


def webdav_config():
    return Config(
        destination="webdav",
        webdav_url="https://cloud.example.com/remote.php/dav/files/alice",
        webdav_username="alice",
        webdav_password="secret",
        webdav_root_path="/Vault",
    )


def test_webdav_cancel_happens_before_transcription(tmp_path, monkeypatch):
    source = tmp_path / "class.mp3"
    source.write_bytes(b"audio")
    monkeypatch.setattr(main, "_load_runtime_config", lambda args: webdav_config())
    monkeypatch.setattr(
        main, "select_webdav_folder", lambda client, media, filenames: None
    )
    monkeypatch.setattr(
        main,
        "TranscriptionService",
        lambda *_: pytest.fail("Whisper service must not be created after cancel"),
    )
    assert main.cmd_transcribe(args_for(source)) == 1


def test_failed_webdav_upload_retains_markdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / "transcripts" / ".staging" / "x" / "class.txt"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("text")
    item = MediaItem(
        str(tmp_path / "class.mp3"),
        "class.mp3",
        1,
        "local",
        datetime.now(timezone.utc),
        local_path=tmp_path / "class.mp3",
    )

    class FailingClient:
        def upload_atomic(self, *args, **kwargs):
            raise RuntimeError("offline")

    with pytest.raises(RuntimeError, match="offline"):
        main._publish_result(
            item,
            TranscriptionResult(
                True, "hello", str(transcript), transcript, duration_seconds=1
            ),
            None,
            "webdav",
            webdav_config(),
            None,
            FailingClient(),
            main.WebDAVTarget("/Vault/Media/class.mp3.md", False, False),
        )
    recovered = list((tmp_path / "transcripts" / "webdav-failed").glob("*.md"))
    assert len(recovered) == 1
    assert "hello" in recovered[0].read_text()


def test_logging_redacts_webdav_secret_values(caplog):
    config = webdav_config()
    main.setup_logging("INFO", config)
    with caplog.at_level("INFO"):
        main.logger.info("credentials %s %s", "alice", "secret")
    assert "alice" not in caplog.text
    assert "secret" not in caplog.text
    assert "***REDACTED***" in caplog.text


def test_study_dispatch_imports_bootstrap_only_when_invoked(monkeypatch):
    called = []
    bootstrap = ModuleType("study.bootstrap")
    bootstrap.run_study = lambda env: called.append(env) or 0
    monkeypatch.setitem(sys.modules, "study.bootstrap", bootstrap)

    parser = main.create_parser()
    args = parser.parse_args(["--env", "academic.env", "study"])

    assert args.func(args) == 0
    assert called == ["academic.env"]
