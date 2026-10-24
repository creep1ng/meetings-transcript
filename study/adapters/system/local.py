"""Hashing, UTC time, and desktop opening through narrow ports."""

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


class LocalFileHasher:
    def sha256(self, path: str) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class LocalClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class LocalFileOpener:
    def __init__(self) -> None:
        if sys.platform == "darwin":
            command = shutil.which("open")
        elif os.name == "nt":
            command = None
        else:
            command = shutil.which("xdg-open")
        self._command = command

    @property
    def available(self) -> bool:
        if os.name == "nt":
            return hasattr(os, "startfile")
        return self._command is not None and bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )

    def open(self, path: str) -> None:
        target = str(Path(path).resolve(strict=True))
        if not self.available:
            raise RuntimeError("OS file opening is unavailable in this session")
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
            return
        assert self._command is not None
        subprocess.Popen(
            [self._command, target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
