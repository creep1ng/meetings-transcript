"""Errors with stable meanings across study workflow boundaries."""


class StudyError(Exception):
    """Base error for the study workflow."""


class DomainValidationError(StudyError, ValueError):
    """A domain value or command violates a study invariant."""


class InvalidNameError(DomainValidationError):
    """A filesystem name is unsafe or non-portable."""


class InvalidClassDateError(DomainValidationError):
    """A class date is not a strict, valid ISO calendar date."""


class InvalidLocationError(DomainValidationError):
    """A catalog location is not a safe relative identifier."""


class InvalidUrlError(DomainValidationError):
    """A link asset is not a supported HTTP(S) URL."""


class CatalogError(StudyError):
    """Base error for source and library catalog operations."""


class RootEscapeError(CatalogError):
    """A path resolves outside its configured root."""


class LibrarySubtreeError(CatalogError):
    """A source location resolves inside the managed library."""


class LocationNotFoundError(CatalogError, FileNotFoundError):
    """A requested catalog location does not exist."""


class LocationNotDirectoryError(CatalogError, NotADirectoryError):
    """A requested catalog location is not a directory."""


class CourseNotFoundError(CatalogError):
    """A requested direct-child course does not exist."""


class NameCollisionError(CatalogError):
    """Names collide under the portable case-insensitive policy."""
