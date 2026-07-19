import pytest
from textual.widgets import Input, Label, ListView, Static

from folder_selector import FolderSelectorApp, rank_folders
from webdav_client import WebDAVResource


def test_fuzzy_search_prefers_basename_and_is_stable_top_twenty():
    folders = [
        WebDAVResource(f"/Vault/Archive/Meeting {index:02d}", True)
        for index in range(25)
    ]
    folders.append(WebDAVResource("/Vault/Meeting", True))
    ranked = rank_folders(folders, "Meeting")
    assert ranked[0].path == "/Vault/Meeting"
    assert len(ranked) == 20
    assert [folder.path for folder in ranked] == [
        folder.path for folder in rank_folders(folders, "Meeting")
    ]


class FakeClient:
    root_path = "/Vault"

    def index_folders(self):
        return [
            WebDAVResource("/Vault", True),
            WebDAVResource("/Vault/Classes", True),
            WebDAVResource("/Vault/Meetings", True),
        ]


@pytest.mark.asyncio
async def test_app_indexes_and_live_searches_with_slash_binding():
    app = FolderSelectorApp(FakeClient(), "Media")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Indexed 3 folders" in str(app.query_one("#status", Label).render())
        await pilot.press("slash")
        search = app.query_one("#search", Input)
        assert search.has_focus
        await pilot.press("M", "e", "e", "t")
        await pilot.pause()
        assert app.result_paths[0] == "/Vault/Meetings"
        assert len(app.query_one("#results", ListView).children) == 3


class FailingClient(FakeClient):
    def index_folders(self):
        raise RuntimeError("index unavailable")


@pytest.mark.asyncio
async def test_app_aborts_on_incomplete_index():
    app = FolderSelectorApp(FailingClient(), "Media")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.index_error == "index unavailable"
        assert app.query_one("#search", Input).disabled


@pytest.mark.asyncio
async def test_selection_confirms_exact_media_destination():
    app = FolderSelectorApp(FakeClient(), "Media")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._confirm("/Vault/Classes")
        await pilot.pause()
        path = app.screen.query_one("#confirmation-path", Static)
        assert "/Vault/Classes/Media" in str(path.render())
        await pilot.click("#confirm")
        await pilot.pause()
        assert app.return_value == "/Vault/Classes"
