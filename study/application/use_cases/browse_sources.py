"""Browse a configured, root-confined source catalog."""

from study.application.dto import BrowseSourcesQuery, SourceDirectoryDTO
from study.application.ports.source import SourceRepository


class BrowseSources:
    def __init__(self, repository: SourceRepository) -> None:
        self._repository = repository

    def execute(self, query: BrowseSourcesQuery | None = None) -> SourceDirectoryDTO:
        location = query.location if query and query.location is not None else None
        return self._repository.browse(
            self._repository.start_location if location is None else location
        )

    __call__ = execute
