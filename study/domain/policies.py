"""Pure naming, filtering, classification, and suggestion policies."""

import re
from collections.abc import Iterable
from pathlib import PurePath
from urllib.parse import (
    SplitResult,
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from .errors import InvalidUrlError
from .models import AssetKind
from .value_objects import AssetFilename, ClassDate, CourseName, name_collision_key

AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
)
VIDEO_EXTENSIONS = frozenset(
    {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}
)
DOCUMENT_EXTENSIONS = frozenset(
    {".doc", ".docx", ".md", ".odt", ".pdf", ".rtf", ".tex", ".txt"}
)
PRESENTATION_EXTENSIONS = frozenset({".key", ".odp", ".ppt", ".pptx"})
SPREADSHEET_EXTENSIONS = frozenset({".csv", ".ods", ".xls", ".xlsx"})
IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
)
ARCHIVE_EXTENSIONS = frozenset(
    {".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip"}
)
INCOMPLETE_SUFFIXES = (
    ".crdownload",
    ".download",
    ".part",
    ".partial",
    ".temp",
    ".tmp",
)
_ISO_DATE_IN_TEXT = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_CREDENTIAL_QUERY = re.compile(
    r"(?:^|[_-])(token|secret|password|passwd|credential|api[_-]?key|auth|signature|sig|key[_-]?pair[_-]?id)(?:$|[_-])",
    re.IGNORECASE,
)


def is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def is_incomplete_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(INCOMPLETE_SUFFIXES) or lowered.endswith("~")


def normalize_url(value: str) -> str:
    """Normalize an HTTP(S) URL without fetching it."""
    if not isinstance(value, str):
        raise InvalidUrlError("URL must be text")
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise InvalidUrlError("link must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidUrlError("link URLs cannot contain credentials")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(_CREDENTIAL_QUERY.search(key) for key, _ in query):
        raise InvalidUrlError("link URL cannot contain credential-like query values")
    try:
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as error:
        raise InvalidUrlError("link URL has an invalid host or port") from error
    if ":" in host:
        host = f"[{host}]"
    default_port = (parsed.scheme.casefold(), port) in {("http", 80), ("https", 443)}
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=")
    normalized = SplitResult(
        parsed.scheme.casefold(),
        netloc,
        path,
        urlencode(sorted(query), doseq=True),
        "",
    )
    return urlunsplit(normalized)


def classify_asset(value: str) -> AssetKind:
    if value.casefold().startswith(("http://", "https://")):
        try:
            normalize_url(value)
        except InvalidUrlError:
            pass
        else:
            return AssetKind.LINK
    extension = PurePath(value).suffix.casefold()
    extension_groups = (
        (AUDIO_EXTENSIONS, AssetKind.AUDIO),
        (VIDEO_EXTENSIONS, AssetKind.VIDEO),
        (DOCUMENT_EXTENSIONS, AssetKind.DOCUMENT),
        (PRESENTATION_EXTENSIONS, AssetKind.PRESENTATION),
        (SPREADSHEET_EXTENSIONS, AssetKind.SPREADSHEET),
        (IMAGE_EXTENSIONS, AssetKind.IMAGE),
        (ARCHIVE_EXTENSIONS, AssetKind.ARCHIVE),
    )
    if extension == ".ipynb":
        return AssetKind.NOTEBOOK
    for extensions, kind in extension_groups:
        if extension in extensions:
            return kind
    return AssetKind.OTHER


def prefixed_asset_name(
    class_date: ClassDate, original: AssetFilename | str
) -> AssetFilename:
    filename = (
        original if isinstance(original, AssetFilename) else AssetFilename(original)
    )
    prefix = f"{class_date.iso} - "
    if filename.value.startswith(prefix):
        return filename
    return AssetFilename(f"{prefix}{filename.value}")


def suggest_class_date(text: str) -> ClassDate | None:
    """Suggest only one unambiguous valid ISO date found in text."""
    candidates: set[ClassDate] = set()
    for match in _ISO_DATE_IN_TEXT.finditer(text):
        try:
            candidates.add(ClassDate.parse(match.group(1)))
        except ValueError:
            continue
    return next(iter(candidates)) if len(candidates) == 1 else None


def suggest_course_name(text: str, courses: Iterable[CourseName]) -> CourseName | None:
    """Suggest a course only when normalized matching is unambiguous."""
    haystack = name_collision_key(text)
    matches = {course for course in courses if course.collision_key in haystack}
    return next(iter(matches)) if len(matches) == 1 else None
