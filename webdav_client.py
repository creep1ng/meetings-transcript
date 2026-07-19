"""Small, root-confined Nextcloud WebDAV client."""

import logging
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Iterable, Optional
from urllib.parse import quote, unquote, urlsplit

import requests

from config import Config

logger = logging.getLogger(__name__)

DAV = "{DAV:}"
PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getlastmodified/><d:getetag/></d:prop></d:propfind>"""


class WebDAVError(RuntimeError):
    """A sanitized, actionable WebDAV operation error."""


@dataclass(frozen=True)
class WebDAVResource:
    path: str
    is_collection: bool
    modified_at: Optional[datetime] = None
    etag: Optional[str] = None


class WebDAVClient:
    """Nextcloud WebDAV operations constrained to one configured root."""

    def __init__(self, config: Config, session: Optional[requests.Session] = None):
        self.config = config
        self.base_url = (config.webdav_url or "").rstrip("/")
        self.base_path = unquote(urlsplit(self.base_url).path).rstrip("/")
        self.root_path = config.webdav_root_path
        self.session = session or requests.Session()
        self.session.auth = (config.webdav_username or "", config.webdav_password or "")
        self.session.headers.update({"User-Agent": "meetings-transcript/1"})

    def list_folder(self, path: str) -> list[WebDAVResource]:
        normalized = self.normalize_path(path)
        response = self._request(
            "PROPFIND",
            normalized,
            expected={207},
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=PROPFIND_BODY.encode(),
        )
        resources = self._parse_multistatus(response.content)
        return [resource for resource in resources if resource.path != normalized]

    def get_metadata(self, path: str) -> Optional[WebDAVResource]:
        normalized = self.normalize_path(path)
        response = self._request(
            "PROPFIND",
            normalized,
            expected={207, 404},
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data=PROPFIND_BODY.encode(),
        )
        if response.status_code == 404:
            return None
        resources = self._parse_multistatus(response.content)
        return next(
            (resource for resource in resources if resource.path == normalized), None
        )

    def create_collection(self, path: str) -> None:
        normalized = self.normalize_path(path)
        response = self._request("MKCOL", normalized, expected={201, 405})
        if response.status_code == 405:
            existing = self.get_metadata(normalized)
            if existing is None or not existing.is_collection:
                raise WebDAVError(
                    f"WebDAV path exists but is not a folder: {normalized}"
                )

    def index_folders(self) -> list[WebDAVResource]:
        """Build a complete deduplicated breadth-first folder index."""
        root = WebDAVResource(self.root_path, True)
        indexed = [root]
        queue = deque([root.path])
        seen = {root.path}
        while queue:
            parent = queue.popleft()
            for resource in self.list_folder(parent):
                if not resource.is_collection or resource.path in seen:
                    continue
                seen.add(resource.path)
                indexed.append(resource)
                queue.append(resource.path)
        return indexed

    def upload_atomic(
        self,
        path: str,
        content: bytes,
        overwrite: bool,
        preflight_exists: Optional[bool] = None,
        expected_etag: Optional[str] = None,
        expected_modified_at: Optional[datetime] = None,
    ) -> None:
        target = self.normalize_path(path)
        parent = str(PurePosixPath(target).parent)
        temporary = self.normalize_path(
            str(
                PurePosixPath(parent)
                / f".{PurePosixPath(target).name}.{uuid.uuid4().hex}.tmp"
            )
        )
        self._request(
            "PUT",
            temporary,
            expected={200, 201, 204},
            headers={"Content-Type": "text/markdown; charset=utf-8"},
            data=content,
        )
        try:
            if preflight_exists is not None:
                current = self.get_metadata(target)
                changed = (
                    current is not None if not preflight_exists else current is None
                )
                if current is not None and preflight_exists:
                    if expected_etag and current.etag != expected_etag:
                        changed = True
                    if (
                        not expected_etag
                        and expected_modified_at
                        and current.modified_at != expected_modified_at
                    ):
                        changed = True
                if changed:
                    raise WebDAVError(
                        f"WebDAV target changed after preflight and was not overwritten: {target}"
                    )
            response = self._request(
                "MOVE",
                temporary,
                expected={201, 204, 409, 412},
                headers={
                    "Destination": self._url_for(target),
                    "Overwrite": "T" if overwrite else "F",
                },
            )
            if response.status_code in (409, 412):
                raise WebDAVError(
                    f"WebDAV target changed after preflight and was not overwritten: {target}"
                )
        except Exception:
            self._cleanup_temporary(temporary)
            raise

    def normalize_path(self, path: str) -> str:
        raw = path.replace("\\", "/")
        if ".." in raw.split("/"):
            raise WebDAVError("WebDAV path traversal is not allowed.")
        normalized = str(PurePosixPath("/", raw.lstrip("/")))
        root = self.root_path.rstrip("/") or "/"
        if root != "/" and normalized != root and not normalized.startswith(root + "/"):
            raise WebDAVError(f"WebDAV path is outside configured root: {normalized}")
        return normalized.rstrip("/") or "/"

    def _url_for(self, path: str) -> str:
        normalized = self.normalize_path(path)
        encoded = "/".join(
            quote(part, safe="") for part in normalized.split("/") if part
        )
        return f"{self.base_url}/{encoded}" if encoded else self.base_url + "/"

    def _request(
        self,
        method: str,
        path: str,
        expected: set[int],
        headers: Optional[dict[str, str]] = None,
        data: Optional[bytes] = None,
    ) -> requests.Response:
        url = self._url_for(path)
        response: Optional[requests.Response] = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    timeout=self.config.webdav_timeout,
                )
            except requests.RequestException as exc:
                if attempt >= self.config.max_retries:
                    raise WebDAVError(
                        f"WebDAV {method} transport failure: {exc.__class__.__name__}"
                    ) from exc
                self._sleep(attempt)
                continue
            if response.status_code in expected:
                return response
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt < self.config.max_retries:
                    self._sleep(attempt)
                    continue
            raise self._http_error(method, path, response.status_code)
        raise WebDAVError(f"WebDAV {method} failed without a response.")

    def _parse_multistatus(self, content: bytes) -> list[WebDAVResource]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise WebDAVError("WebDAV returned malformed XML.") from exc

        resources: list[WebDAVResource] = []
        for response in root.findall(f"{DAV}response"):
            href = response.findtext(f"{DAV}href")
            if not href:
                continue
            successful_prop = None
            for propstat in response.findall(f"{DAV}propstat"):
                status = propstat.findtext(f"{DAV}status", "")
                if " 200 " in status:
                    successful_prop = propstat.find(f"{DAV}prop")
                    break
            if successful_prop is None:
                continue
            path = self._path_from_href(href)
            resource_type = successful_prop.find(f"{DAV}resourcetype")
            is_collection = (
                resource_type is not None
                and resource_type.find(f"{DAV}collection") is not None
            )
            modified = successful_prop.findtext(f"{DAV}getlastmodified")
            etag = successful_prop.findtext(f"{DAV}getetag")
            resources.append(
                WebDAVResource(
                    path=path,
                    is_collection=is_collection,
                    modified_at=_parse_http_date(modified),
                    etag=etag.strip('"') if etag else None,
                )
            )
        return _deduplicate(resources)

    def _path_from_href(self, href: str) -> str:
        href_path = unquote(urlsplit(href).path).rstrip("/")
        if href_path == self.base_path:
            remote = "/"
        elif href_path.startswith(self.base_path + "/"):
            remote = href_path[len(self.base_path) :]
        else:
            raise WebDAVError("WebDAV response referenced a path outside its endpoint.")
        return self.normalize_path(remote)

    def _cleanup_temporary(self, path: str) -> None:
        try:
            self._request("DELETE", path, expected={204, 404})
        except WebDAVError:
            logger.warning("Could not remove a temporary WebDAV upload artifact.")

    def _sleep(self, attempt: int) -> None:
        time.sleep(self.config.retry_backoff_base**attempt)

    @staticmethod
    def _http_error(method: str, path: str, status: int) -> WebDAVError:
        explanations = {
            401: "authentication failed; verify the username and app password",
            403: "permission denied",
            404: "resource not found",
            507: "storage quota exceeded",
        }
        detail = explanations.get(status, f"HTTP {status}")
        return WebDAVError(f"WebDAV {method} failed for {path}: {detail}.")


def _parse_http_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _deduplicate(resources: Iterable[WebDAVResource]) -> list[WebDAVResource]:
    by_path: dict[str, WebDAVResource] = {}
    for resource in resources:
        by_path[resource.path.rstrip("/") or "/"] = resource
    return list(by_path.values())
