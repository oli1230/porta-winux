"""Interactive view for `porta-winux compare -i` (Textual, optional).

Left: the change tree, collapsed to the profile roots; Enter/Space expands,
`a` expands everything, `c` collapses. Right: details of the highlighted
entry — size/mtime on each side, and for text files `d` shows a unified
content diff (snapshot contents come from `restic dump`, live contents from
disk). `f` filters the tree to one status (added/removed/modified/moved/
duplicate). `q` quits.
"""

from __future__ import annotations

import difflib
from datetime import datetime
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, Tree

from .compare import GLYPH, TreeNode, summarize

STATUS_STYLE = {
    "added": "green", "removed": "red", "modified": "yellow",
    "type": "magenta", "moved": "cyan", "duplicate": "magenta",
}
FILTER_CYCLE = [None, "added", "removed", "modified", "moved", "duplicate"]


def _fmt_time(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "-"


class CompareApp(App):
    CSS = """
    #tree { width: 55%; }
    #detail { width: 45%; border-left: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        Binding("q,escape", "quit", "quit"),
        Binding("a", "expand_all", "expand all"),
        Binding("c", "collapse_all", "collapse"),
        Binding("d", "content_diff", "content diff"),
        Binding("f", "cycle_filter", "filter status"),
    ]

    def __init__(
        self,
        tree: TreeNode,
        title: str,
        side_a: str,
        side_b: str,
        read_a: Callable[[str], bytes] | None = None,
        read_b: Callable[[str], bytes] | None = None,
    ):
        super().__init__()
        self.change_tree = tree
        self.heading = title
        self.side_a, self.side_b = side_a, side_b
        self.read_a, self.read_b = read_a, read_b
        self.status_filter: str | None = None
        self.current: TreeNode | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield Tree("changes", id="tree")
            with Vertical(id="detail"):
                yield Static("", id="detail_text")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"porta-winux compare  {self.heading}"
        self._fill()

    # -- tree building -----------------------------------------------------------

    def _fill(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.clear()
        tree.root.label = f"{self.side_a} -> {self.side_b}" + (
            f"   [filter: {self.status_filter}]" if self.status_filter else "")
        for root in sorted(self.change_tree.children.values(), key=lambda k: k.name):
            if self._hidden(root):
                continue
            node = tree.root.add(self._label(root, top=True), data=root, expand=True)
            self._add_children(node, root)
        tree.root.expand()
        tree.focus()

    def _hidden(self, n: TreeNode) -> bool:
        if not self.status_filter:
            return False
        if n.change and n.change.status == self.status_filter:
            return False
        return not n.counts.get(self.status_filter)

    def _add_children(self, widget_node, n: TreeNode) -> None:
        for k in sorted(n.children.values(), key=lambda x: (not x.children, x.name)):
            if self._hidden(k):
                continue
            if k.children:
                child = widget_node.add(self._label(k), data=k, expand=False)
                self._add_children(child, k)
            else:
                widget_node.add_leaf(self._label(k), data=k)

    def _label(self, n: TreeNode, top: bool = False):
        from rich.text import Text
        name = n.path if top else n.name
        t = Text()
        if n.change:
            c = n.change
            t.append(f"{GLYPH[c.status]} ", style=STATUS_STYLE[c.status])
            t.append(name + ("/" if c.is_dir else ""))
            if c.status == "moved":
                t.append(f"  -> {c.other}", style="cyan")
            elif c.status == "duplicate":
                t.append(f"  == {c.other}", style="magenta")
            if n.counts and c.is_dir:
                t.append(f"  ({summarize(n.counts)})", style="dim")
        else:
            t.append(name + "/")
            t.append(f"  ({summarize(n.counts)})", style="dim")
        return t

    # -- interaction ---------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        n = event.node.data
        if isinstance(n, TreeNode):
            self.current = n
            self._show_detail(n)

    def _show_detail(self, n: TreeNode) -> None:
        c = n.change
        if not c:
            text = f"{n.path}/\n\n{summarize(n.counts)} below"
        else:
            lines = [f"{n.path}", f"status: {c.status}"]
            if c.other:
                lines.append(f"other:  {c.other}")
            for side, e in ((self.side_a, c.a), (self.side_b, c.b)):
                if e is None:
                    lines.append(f"\n{side}: (absent)")
                else:
                    lines.append(f"\n{side}: {e.type}")
                    if e.type == "file":
                        lines.append(f"  size  {e.size}")
                    if e.type == "symlink":
                        lines.append(f"  ->    {e.target}")
                    lines.append(f"  mtime {_fmt_time(e.mtime)}")
            if c.files:
                lines.append(f"\n{c.files} files")
            if c.status == "modified" and c.a and c.a.type == "file":
                lines.append("\npress d for a content diff")
            text = "\n".join(lines)
        self.query_one("#detail_text", Static).update(text)

    def action_expand_all(self) -> None:
        self.query_one("#tree", Tree).root.expand_all()

    def action_collapse_all(self) -> None:
        tree = self.query_one("#tree", Tree)
        for child in tree.root.children:
            child.collapse_all()
            child.expand()

    def action_cycle_filter(self) -> None:
        i = FILTER_CYCLE.index(self.status_filter)
        self.status_filter = FILTER_CYCLE[(i + 1) % len(FILTER_CYCLE)]
        self._fill()

    def action_content_diff(self) -> None:
        n = self.current
        if not n or not n.change or n.change.status not in ("modified", "added", "removed"):
            self.notify("select a modified/added/removed file first")
            return
        c = n.change
        out = self.query_one("#detail_text", Static)
        try:
            a = self.read_a(n.path) if (self.read_a and c.a and c.a.type == "file") else b""
            b = self.read_b(n.path) if (self.read_b and c.b and c.b.type == "file") else b""
        except Exception as e:  # never crash the viewer
            out.update(f"(cannot read contents: {e})")
            return
        if b"\x00" in a[:8192] or b"\x00" in b[:8192]:
            out.update(f"{n.path}\n\n(binary file: {len(a)} -> {len(b)} bytes)")
            return
        diff = difflib.unified_diff(
            a.decode("utf-8", "replace").splitlines(), b.decode("utf-8", "replace").splitlines(),
            fromfile=self.side_a, tofile=self.side_b, lineterm="", n=3,
        )
        text = "\n".join(list(diff)[:2000]) or "(contents identical; only metadata differs)"
        out.update(text)


def run_compare_tui(tree: TreeNode, title: str, side_a: str, side_b: str,
                    read_a=None, read_b=None) -> int:
    CompareApp(tree, title, side_a, side_b, read_a, read_b).run()
    return 0
