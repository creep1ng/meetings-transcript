from dataclasses import dataclass

import pytest
import requests

from config import Config
from webdav_client import WebDAVClient, WebDAVError, WebDAVResource


MULTISTATUS = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/remote.php/dav/files/alice/Vault/</d:href><d:propstat>
    <d:prop><d:resourcetype><d:collection/></d:resourcetype><d:getetag>\"root\"</d:getetag></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
  <d:response><d:href>/remote.php/dav/files/alice/Vault/Caf%C3%A9/</d:href><d:propstat>
    <d:prop><d:resourcetype><d:collection/></d:resourcetype><d:getlastmodified>Wed, 01 Jul 2026 12:00:00 GMT</d:getlastmodified></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""


@dataclass
class Call:
    method: str
    url: str
    kwargs: dict


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.auth = None
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append(Call(method, url, kwargs))
        status, content = next(self.responses)
        response = requests.Response()
        response.status_code = status
        response._content = content
        return response


def make_config():
    return Config(
        destination="webdav",
        webdav_url="https://cloud.example.com/remote.php/dav/files/alice",
        webdav_username="alice",
        webdav_password="secret",
        webdav_root_path="/Vault",
        max_retries=0,
    )


def test_propfind_parses_unicode_collection_and_http_date():
    session = FakeSession([(207, MULTISTATUS)])
    client = WebDAVClient(make_config(), session)
    folders = client.list_folder("/Vault")
    assert folders[0].path == "/Vault/Café"
    assert folders[0].is_collection
    assert folders[0].modified_at.isoformat() == "2026-07-01T12:00:00+00:00"


def test_urls_quote_each_segment_and_reject_traversal():
    client = WebDAVClient(make_config(), FakeSession([]))
    assert client._url_for("/Vault/Café notes") == (
        "https://cloud.example.com/remote.php/dav/files/alice/Vault/Caf%C3%A9%20notes"
    )
    with pytest.raises(WebDAVError, match="traversal"):
        client.get_metadata("/Vault/../Secret")
    with pytest.raises(WebDAVError, match="outside"):
        client.get_metadata("/Other")


@pytest.mark.parametrize(("overwrite", "header"), [(False, "F"), (True, "T")])
def test_atomic_upload_uses_explicit_overwrite_header(overwrite, header):
    session = FakeSession([(201, b""), (201, b"")])
    client = WebDAVClient(make_config(), session)
    client.upload_atomic("/Vault/Media/class.mp3.md", b"text", overwrite)
    assert [call.method for call in session.calls] == ["PUT", "MOVE"]
    assert session.calls[1].kwargs["headers"]["Overwrite"] == header


def test_atomic_upload_reports_race_and_cleans_temp():
    session = FakeSession([(201, b""), (412, b""), (204, b"")])
    client = WebDAVClient(make_config(), session)
    with pytest.raises(WebDAVError, match="changed after preflight"):
        client.upload_atomic("/Vault/Media/class.mp3.md", b"text", False)
    assert [call.method for call in session.calls] == ["PUT", "MOVE", "DELETE"]


def test_atomic_upload_rechecks_absent_target_before_move():
    target_exists = MULTISTATUS.replace(
        b"/remote.php/dav/files/alice/Vault/",
        b"/remote.php/dav/files/alice/Vault/Media/class.mp3.md",
        1,
    )
    session = FakeSession([(201, b""), (207, target_exists), (204, b"")])
    client = WebDAVClient(make_config(), session)
    with pytest.raises(WebDAVError, match="changed after preflight"):
        client.upload_atomic(
            "/Vault/Media/class.mp3.md",
            b"text",
            False,
            preflight_exists=False,
        )
    assert [call.method for call in session.calls] == ["PUT", "PROPFIND", "DELETE"]


def test_auth_error_is_actionable_without_secret():
    client = WebDAVClient(make_config(), FakeSession([(401, b"secret response")]))
    with pytest.raises(WebDAVError, match="app password") as error:
        client.get_metadata("/Vault/file")
    assert "secret" not in str(error.value)


def test_recursive_index_is_breadth_first_and_deduplicated():
    client = WebDAVClient(make_config(), FakeSession([]))
    calls = []
    mapping = {
        "/Vault": [
            WebDAVResource("/Vault/A", True),
            WebDAVResource("/Vault/A", True),
            WebDAVResource("/Vault/file.mp3", False),
            WebDAVResource("/Vault/B", True),
        ],
        "/Vault/A": [WebDAVResource("/Vault/A/Child", True)],
        "/Vault/B": [],
        "/Vault/A/Child": [],
    }

    def list_folder(path):
        calls.append(path)
        return mapping[path]

    client.list_folder = list_folder
    indexed = client.index_folders()
    assert [resource.path for resource in indexed] == [
        "/Vault",
        "/Vault/A",
        "/Vault/B",
        "/Vault/A/Child",
    ]
    assert calls == ["/Vault", "/Vault/A", "/Vault/B", "/Vault/A/Child"]
