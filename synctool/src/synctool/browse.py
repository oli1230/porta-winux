"""Interactive browser (Textual): fuzzy-filter files in a snapshot,
preview them, and press Enter to check one out and open $EDITOR.

Runs in any terminal — including a console on a half-installed machine
with no desktop environment, which is the whole point.
"""

from __future__ import annotations

import os
import subprocess

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from .manifest import DriveLayout
from .store import SnapshotStore
from . import workspace as ws

PREVIEW_BYTES = 8192


def _fuzzy(needle: str, haystack: str) -> bool:
    """Subsequence match, like fzf's default."""
    it = iter(haystack.lower())
    return all(ch in it for ch in needle.lower())


class Browser(App):
    CSS = """
    #filter { dock: top; }
    #files { width: 45%; }
    #preview { width: 55%; border-left: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape,q", "quit", "quit"),
        Binding("enter", "edit", "checkout + edit", priority=True),
        Binding("down,ctrl+n", "next", "next", show=False),
        Binding("up,ctrl+p", "prev", "prev", show=False),
    ]

    def __init__(self, store: SnapshotStore, layout: DriveLayout, snapshot_id: str):
        super().__init__()
        self.store = store
        self.layout = layout
        self.snapshot_id = snapshot_id
        self.all_files: list[str] = []
        self.shown: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Input(placeholder="fuzzy filter…", id="filter")
        with Horizontal():
            yield ListView(id="files")
            with Vertical(id="preview"):
                yield Static("", id="preview_text")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"synctool browse  [{self.snapshot_id[:8]}]"
        self.all_files = [
            n["path"] for n in self.store.ls(self.snapshot_id) if n.get("type") == "file"
        ]
        self._refill("")
        self.query_one("#filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refill(event.value)

    def _refill(self, needle: str) -> None:
        lv = self.query_one("#files", ListView)
        lv.clear()
        self.shown = [p for p in self.all_files if _fuzzy(needle, p)][:500]
        for p in self.shown:
            lv.append(ListItem(Static(p)))
        if self.shown:
            lv.index = 0
            self._preview(self.shown[0])
        else:
            self.query_one("#preview_text", Static).update("(no matches)")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = self.query_one("#files", ListView).index
        if idx is not None and 0 <= idx < len(self.shown):
            self._preview(self.shown[idx])

    def _preview(self, path: str) -> None:
        try:
            data = self.store.dump(self.snapshot_id, path)[:PREVIEW_BYTES]
            text = data.decode("utf-8", errors="replace")
        except Exception as e:  # preview must never crash the app
            text = f"(preview unavailable: {e})"
        if "\x00" in text:
            text = "(binary file)"
        self.query_one("#preview_text", Static).update(text)

    def action_next(self) -> None:
        self.query_one("#files", ListView).action_cursor_down()

    def action_prev(self) -> None:
        self.query_one("#files", ListView).action_cursor_up()

    def action_edit(self) -> None:
        idx = self.query_one("#files", ListView).index
        if idx is None or not (0 <= idx < len(self.shown)):
            return
        path = self.shown[idx]
        ws.checkout(self.store, self.layout, self.snapshot_id, [path])
        local = self.layout.workspace_root / path.lstrip("/")
        editor = os.environ.get("EDITOR", "nano")
        with self.suspend():
            subprocess.run([editor, str(local)])
        self.notify(f"staged in workspace: {path}\ncommit with: synctool commit -m '…'")


def run_browser(store: SnapshotStore, layout: DriveLayout, snapshot_id: str) -> int:
    Browser(store, layout, snapshot_id).run()
    staged = ws.status(layout)
    if staged:
        print("workspace contains staged edits:")
        for f in staged:
            print(f"  {f}")
        print("run: synctool commit -m 'what changed'")
    return 0
