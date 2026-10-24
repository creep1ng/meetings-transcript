"""Source browser port."""

from typing import Protocol

from study.application.dto import SourceDirectoryDTO


class SourceRepository(Protocol):
    @property
    def start_location(self) -> str: ...

    def browse(self, location: str) -> SourceDirectoryDTO: ...
