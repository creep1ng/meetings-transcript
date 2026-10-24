"""Small operating-system boundaries used by academic use cases."""

from datetime import datetime
from typing import Protocol


class Hasher(Protocol):
    def sha256(self, path: str) -> str: ...


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class FileOpener(Protocol):
    @property
    def available(self) -> bool: ...

    def open(self, path: str) -> None: ...
