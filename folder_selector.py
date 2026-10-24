"""Interactive Textual selector for a root-confined WebDAV vault."""

import asyncio
import sys
from pathlib import PurePosixPath
from typing import Optional

from rapidfuzz import fuzz
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    Tree,
)

from webdav_client import WebDAVClient, WebDAVResource


class ConfirmationScreen(ModalScreen[bool]):
    """Confirm the exact remote Media destination."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Back")]

    def __init__(self, destination: str):
        super().__init__()
        self.destination = destination

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation"):
            yield Label("Publish all accepted transcripts to:")
            yield Static(self.destination, id="confirmation-path")
            with Horizontal():
                yield Button("Publish here", variant="primary", id="confirm")
                yield Button("Go back", id="back")

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#back")
    def back(self) -> None:
        self.dismiss(False)


class FolderSelectorApp(App[Optional[str]]):
    """Index, search, and select one WebDAV folder for a batch."""

    CSS = """
    Screen { background: #101820; color: #e8e5da; }
    Header { background: #d97706; color: #101820; }
    #body { height: 1fr; padding: 1 2; }
    #search { border: tall #d97706; margin-bottom: 1; }
    #panes { height: 1fr; }
    #tree, #results { width: 1fr; border: round #52606d; }
    #tree { margin-right: 1; }
    #status { height: 1; color: #fbbf24; }
    #confirmation { width: 72; height: auto; padding: 2 3; border: thick #d97706; background: #18242e; }
    #confirmation-path { margin: 1 0 2 0; color: #fbbf24; }
    #confirmation Horizontal { height: 3; align: center middle; }
    Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "back_or_cancel", "Back / Cancel"),
    ]

    def __init__(
        self,
        client: WebDAVClient,
        media_subfolder: str,
        filenames: tuple[str, ...] = (),
    ):
        super().__init__()
        self.client = client
        self.media_subfolder = media_subfolder
        self.filenames = filenames
        self.folders: list[WebDAVResource] = []
        self.result_paths: list[str] = []
        self.index_error: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="body"):
            if self.filenames:
                label = "File" if len(self.filenames) == 1 else "Files"
                yield Label(
                    f"{label} to transcribe: {', '.join(self.filenames)}",
                    id="source-files",
                )
            yield Input(
                placeholder="Press / to fuzzy-search the complete vault", id="search"
            )
            yield Label("Indexing folders...", id="status")
            with Horizontal(id="panes"):
                yield Tree("Vault", id="tree")
                yield ListView(id="results")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "Select transcript destination"
        try:
            self.folders = await asyncio.to_thread(self.client.index_folders)
        except Exception as exc:
            self.index_error = str(exc)
            self.query_one("#status", Label).update(f"Index failed: {exc}")
            self.query_one("#search", Input).disabled = True
            return
        self._populate_tree()
        self._update_results("")
        self.query_one("#status", Label).update(
            f"Indexed {len(self.folders)} folders. Enter selects; / searches."
        )
        self.query_one("#tree", Tree).focus()

    def _populate_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        root_path = self.client.root_path
        tree.root.set_label(PurePosixPath(root_path).name or "/")
        tree.root.data = root_path
        nodes = {root_path: tree.root}
        for resource in sorted(
            self.folders,
            key=lambda folder: (folder.path.count("/"), folder.path.casefold()),
        ):
            if resource.path == root_path:
                continue
            parent_path = str(PurePosixPath(resource.path).parent)
            parent = nodes.get(parent_path, tree.root)
            nodes[resource.path] = parent.add(
                PurePosixPath(resource.path).name, data=resource.path
            )
        tree.root.expand()

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        self._update_results(event.value)

    def _update_results(self, query: str) -> None:
        ranked = rank_folders(self.folders, query)
        self.result_paths = [folder.path for folder in ranked]
        results = self.query_one("#results", ListView)
        results.clear()
        results.extend(ListItem(Label(path)) for path in self.result_paths)

    @on(Tree.NodeSelected, "#tree")
    def tree_selected(self, event: Tree.NodeSelected[str]) -> None:
        if event.node.data:
            self._confirm(event.node.data)

    @on(ListView.Selected, "#results")
    def result_selected(self, event: ListView.Selected) -> None:
        if event.list_view.index is not None and event.list_view.index < len(
            self.result_paths
        ):
            self._confirm(self.result_paths[event.list_view.index])

    def _confirm(self, folder: str) -> None:
        destination = str(PurePosixPath(folder) / self.media_subfolder)
        self.push_screen(
            ConfirmationScreen(destination),
            lambda confirmed: self.exit(folder) if confirmed else None,
        )

    def action_focus_search(self) -> None:
        if not self.index_error:
            self.query_one("#search", Input).focus()

    def action_back_or_cancel(self) -> None:
        search = self.query_one("#search", Input)
        if search.has_focus:
            if search.value:
                search.value = ""
            self.query_one("#tree", Tree).focus()
            return
        self.exit(None)


def rank_folders(folders: list[WebDAVResource], query: str) -> list[WebDAVResource]:
    """Rank basename above full path and return a stable top twenty."""
    normalized = query.strip()
    if not normalized:
        return sorted(folders, key=lambda folder: folder.path.casefold())[:20]

    scored = []
    for folder in folders:
        basename = PurePosixPath(folder.path).name or "/"
        basename_score = fuzz.WRatio(normalized, basename)
        path_score = fuzz.WRatio(normalized, folder.path)
        score = basename_score * 0.7 + path_score * 0.3
        scored.append((score, folder.path.casefold(), folder))
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [entry[2] for entry in scored[:20]]


def select_webdav_folder(
    client: WebDAVClient,
    media_subfolder: str,
    filenames: tuple[str, ...] = (),
) -> Optional[str]:
    """Run the selector only when an interactive terminal is available."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("WebDAV folder selection requires an interactive terminal.")
    return FolderSelectorApp(client, media_subfolder, filenames).run()
