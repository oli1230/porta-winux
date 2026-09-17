"""Compare: what changed between two states of the system.

A "state" is a Listing — every path with its type, size and mtime — built
either from a snapshot (`restic ls --json`, one process) or from the live
filesystem (a scandir walk of the profile paths with the profile's excludes
applied, so `.cache` doesn't show up as thousands of "added" files).

diff_listings() yields Changes with a status:

    added      only on side B
    removed    only on side A
    modified   file on both sides, size or mtime differ
    type       file<->dir<->symlink
    moved      removed on A and added on B with the same signature
               (size, mtime, basename); whole directories collapse to one
               `moved` when every file inside moved to the same place
    duplicate  a directory added on B whose contents are identical to a
               directory that exists on BOTH sides -- the tell-tale sign
               of "moved on machine 1, restored onto machine 2, and now
               the old location is back as a stale copy" (README, item 6)

Signature-based move detection is a heuristic: `mv` preserves mtimes, so a
moved file keeps (size, mtime, name); a file that was moved *and* edited
shows as removed + added, which is honest. confirm_move() can upgrade a
candidate to a certain match using restic's content hashes, at the cost of
one `restic cat tree` per directory, so it is only used on request.

build_tree()/render_tree() turn the flat change list into the collapsible
tree the CLI prints, rooted at the manifest profile paths.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .store import SnapshotStore

# -- listings ------------------------------------------------------------------


@dataclass(slots=True)
class Entry:
    path: str
    type: str          # "file" | "dir" | "symlink" | other
    size: int = 0
    mtime: float = 0.0  # epoch seconds
    target: str = ""    # symlink target

    @property
    def sig(self) -> tuple:
        """Move-detection signature: what `mv` preserves."""
        return (self.type, self.size, int(self.mtime), os.path.basename(self.path))


Listing = dict[str, Entry]


def _parse_time(s: str) -> float:
    if not s:
        return 0.0
    try:
        s2 = s.replace("Z", "+00:00")
        # restic emits nanoseconds; datetime accepts at most 6 fraction digits
        if "." in s2:
            head, tail = s2.split(".", 1)
            frac = "".join(ch for ch in tail if ch.isdigit())[:6]
            tz = tail[len("".join(ch for ch in tail if ch.isdigit())):]
            s2 = f"{head}.{frac}{tz}"
        return datetime.fromisoformat(s2).timestamp()
    except ValueError:
        return 0.0


def listing_from_snapshot(store: SnapshotStore, snapshot_id: str,
                          under: list[str] | None = None) -> Listing:
    """Every node of a snapshot (restricted to the `under` prefixes)."""
    out: Listing = {}
    prefixes = [p.rstrip("/") for p in (under or [])]
    for node in store.ls(snapshot_id):
        path = node.get("path")
        if not path:
            continue
        if prefixes and not any(path == p or path.startswith(p + "/") for p in prefixes):
            continue
        out[path] = Entry(
            path=path,
            type=node.get("type", "file"),
            size=int(node.get("size") or 0),
            mtime=_parse_time(node.get("mtime", "")),
            target=node.get("link_target") or node.get("linktarget") or "",
        )
    return out


def matches_exclude(path: str, patterns: Iterable[str]) -> bool:
    """restic-style exclude matching, simplified:
      /abs/pattern   anchored at the root
      **/name        any depth
      name           matches that path component anywhere
      dir/name       matches any suffix of the path components
    """
    parts = path.strip("/").split("/") if path.strip("/") else []
    for pat in patterns:
        anchored = pat.startswith("/")
        pparts = [x for x in pat.strip("/").split("/") if x]
        if not pparts:
            continue
        if anchored:
            if _match_parts(pparts, parts, prefix=True):
                return True
        else:
            for i in range(len(parts)):
                if _match_parts(pparts, parts[i:], prefix=True):
                    return True
    return False


def _match_parts(pat: list[str], parts: list[str], prefix: bool) -> bool:
    """Match pattern components against path components; `**` spans any
    number of components. With prefix=True a match on a leading segment of
    `parts` counts (a directory pattern excludes its subtree)."""
    if not pat:
        return True if prefix else not parts
    if pat[0] == "**":
        for i in range(len(parts) + 1):
            if _match_parts(pat[1:], parts[i:], prefix):
                return True
        return False
    if not parts:
        return False
    if not fnmatch.fnmatchcase(parts[0], pat[0]):
        return False
    return _match_parts(pat[1:], parts[1:], prefix)


def listing_from_live(paths: list[str], excludes: list[str], root: str = "/") -> Listing:
    """Walk the live filesystem under the given absolute paths (mapped under
    `root`), recording entries keyed by their *system* path (no root prefix)
    so they line up with snapshot paths."""
    out: Listing = {}
    root = root.rstrip("/") or "/"

    def local(p: str) -> str:
        return p if root == "/" else root + p

    def add(sys_path: str, st: os.stat_result, is_link: bool, target: str = "") -> None:
        if is_link:
            t = "symlink"
        elif stat.S_ISDIR(st.st_mode):
            t = "dir"
        elif stat.S_ISREG(st.st_mode):
            t = "file"
        else:
            t = "other"
        out[sys_path] = Entry(path=sys_path, type=t, size=st.st_size if t == "file" else 0,
                              mtime=st.st_mtime, target=target)

    for top in paths:
        top = top.rstrip("/") or "/"
        try:
            st = os.lstat(local(top))
        except OSError:
            continue
        if matches_exclude(top, excludes):
            continue
        add(top, st, stat.S_ISLNK(st.st_mode))
        if not stat.S_ISDIR(st.st_mode):
            continue
        stack = [top]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(local(d)) as it:
                    entries = list(it)
            except OSError:
                continue
            for de in entries:
                sp = f"{d}/{de.name}" if d != "/" else f"/{de.name}"
                if matches_exclude(sp, excludes):
                    continue
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                if de.is_symlink():
                    try:
                        tgt = os.readlink(de.path)
                    except OSError:
                        tgt = ""
                    add(sp, st, True, tgt)
                    continue
                add(sp, st, False)
                if de.is_dir(follow_symlinks=False):
                    stack.append(sp)
    return out


# -- diff ----------------------------------------------------------------------


@dataclass(slots=True)
class Change:
    path: str
    status: str            # added removed modified type moved duplicate
    a: Entry | None = None
    b: Entry | None = None
    other: str = ""        # moved: destination path; duplicate: the original
    files: int = 0         # for collapsed directory changes: files affected

    @property
    def is_dir(self) -> bool:
        e = self.b or self.a
        return bool(e and e.type == "dir")


MTIME_TOLERANCE = 2.0  # seconds; exFAT/FAT round mtimes, NFS may skew


def _same_file(a: Entry, b: Entry) -> bool:
    if a.type != b.type:
        return False
    if a.type == "symlink":
        return a.target == b.target
    if a.type != "file":
        return True
    return a.size == b.size and abs(a.mtime - b.mtime) <= MTIME_TOLERANCE


def diff_listings(a: Listing, b: Listing, detect_moves: bool = True) -> list[Change]:
    changes: list[Change] = []
    for path in sorted(set(a) | set(b)):
        ea, eb = a.get(path), b.get(path)
        if ea and not eb:
            changes.append(Change(path, "removed", ea, None))
        elif eb and not ea:
            changes.append(Change(path, "added", None, eb))
        elif ea.type != eb.type:
            changes.append(Change(path, "type", ea, eb))
        elif not _same_file(ea, eb):
            changes.append(Change(path, "modified", ea, eb))
    if detect_moves:
        changes = _detect_moves(changes, a, b)
        changes = _detect_duplicates(changes, a, b)
    return changes


def _detect_moves(changes: list[Change], a: Listing, b: Listing) -> list[Change]:
    removed = [c for c in changes if c.status == "removed" and c.a and c.a.type == "file"]
    added = [c for c in changes if c.status == "added" and c.b and c.b.type == "file"]
    if not removed or not added:
        return changes
    by_sig_r: dict[tuple, list[Change]] = defaultdict(list)
    by_sig_a: dict[tuple, list[Change]] = defaultdict(list)
    for c in removed:
        by_sig_r[c.a.sig].append(c)
    for c in added:
        by_sig_a[c.b.sig].append(c)
    pairs: dict[str, str] = {}  # old -> new (unique signature matches only)
    for sig, rs in by_sig_r.items():
        as_ = by_sig_a.get(sig)
        if as_ and len(rs) == 1 and len(as_) == 1 and rs[0].a.size > 0:
            pairs[rs[0].path] = as_[0].path
    if not pairs:
        return changes

    # Collapse to directory moves where every file of a removed dir went to
    # one destination dir keeping its relative path.
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for old, new in pairs.items():
        op, np_ = old.split("/"), new.split("/")
        while op and np_ and op[-1] == np_[-1]:
            op.pop()
            np_.pop()
        groups[("/".join(op) or "/", "/".join(np_) or "/")].add(old)
    dir_moves: dict[str, str] = {}
    for (od, nd), olds in groups.items():
        if od == "/" or nd == "/" or od not in a or a[od].type != "dir" or od in b:
            continue
        files_under = {p for p, e in a.items()
                       if e.type == "file" and p.startswith(od + "/")}
        if files_under and files_under <= olds:
            dir_moves[od] = nd

    out: list[Change] = []
    consumed_new = {n for o, n in pairs.items() if o not in _under_any(o, dir_moves)}
    consumed_new |= {p for p in b if any(p == nd or p.startswith(nd + "/")
                                         for nd in dir_moves.values())}
    for c in changes:
        p = c.path
        if p in dir_moves:
            nfiles = sum(1 for q, e in a.items() if e.type == "file" and q.startswith(p + "/"))
            out.append(Change(p, "moved", c.a, b.get(dir_moves[p]), other=dir_moves[p],
                              files=nfiles))
            continue
        if any(p.startswith(od + "/") for od in dir_moves):
            continue
        if c.status == "added" and p in consumed_new:
            continue
        if c.status == "removed" and p in pairs:
            out.append(Change(p, "moved", c.a, b.get(pairs[p]), other=pairs[p]))
            continue
        out.append(c)
    return out


def _under_any(path: str, dirs: dict[str, str]) -> set[str]:
    return {path} if any(path.startswith(d + "/") for d in dirs) else set()


def _dir_signature(listing: Listing, d: str) -> frozenset:
    pre = d + "/"
    return frozenset((p[len(pre):], e.size, int(e.mtime))
                     for p, e in listing.items() if e.type == "file" and p.startswith(pre))


def _dir_stats(listing: Listing) -> dict[str, tuple[int, int]]:
    """(file count, total size) for every directory, computed bottom-up."""
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p, e in listing.items():
        if e.type != "file":
            continue
        d = os.path.dirname(p)
        while d:
            stats[d][0] += 1
            stats[d][1] += e.size
            if d == "/":
                break
            d = os.path.dirname(d)
    return {d: (c, s) for d, (c, s) in stats.items()}


def _detect_duplicates(changes: list[Change], a: Listing, b: Listing) -> list[Change]:
    """An added directory identical to a directory present on both sides is
    flagged `duplicate` (stale copy left behind by a move + restore)."""
    added_dirs = [c.path for c in changes if c.status == "added" and c.is_dir]
    if not added_dirs:
        return changes
    top_added = [d for d in added_dirs if not any(d.startswith(o + "/") for o in added_dirs)]
    stats_b = _dir_stats(b)
    stats_a = _dir_stats(a)
    common_by_stats: dict[tuple[int, int], list[str]] = defaultdict(list)
    for d, st in stats_a.items():
        if d in b and b[d].type == "dir" and st[0] > 0:
            common_by_stats[st].append(d)
    dup: dict[str, str] = {}
    for d in top_added:
        st = stats_b.get(d)
        if not st or st[0] == 0:
            continue
        cands = common_by_stats.get(st) or []
        if not cands:
            continue
        sig = _dir_signature(b, d)
        for c in cands:
            if _dir_signature(b, c) == sig:
                dup[d] = c
                break
    if not dup:
        return changes
    out = []
    for c in changes:
        p = c.path
        if p in dup:
            out.append(Change(p, "duplicate", None, c.b, other=dup[p],
                              files=stats_b[p][0]))
        elif any(p.startswith(d + "/") for d in dup):
            continue
        else:
            out.append(c)
    return out


def confirm_move(store: SnapshotStore, snap_a: str, snap_b: str, old: str, new: str) -> bool:
    """Certain answer for a snapshot-to-snapshot move candidate: compare the
    restic content hashes (files) or subtree ids (dirs). One `cat tree` per
    side, so only call this for candidates the user asks about."""
    def node(snap: str, path: str) -> dict | None:
        parent, name = os.path.dirname(path), os.path.basename(path)
        try:
            return next((n for n in store.tree(snap, parent) if n.get("name") == name), None)
        except Exception:
            return None
    na, nb = node(snap_a, old), node(snap_b, new)
    if not na or not nb:
        return False
    if na.get("subtree") or nb.get("subtree"):
        return bool(na.get("subtree")) and na.get("subtree") == nb.get("subtree")
    return bool(na.get("content")) and na.get("content") == nb.get("content")


# -- tree ----------------------------------------------------------------------

GLYPH = {"added": "+", "removed": "-", "modified": "~", "type": "T",
         "moved": ">", "duplicate": "="}
_COLOR = {"added": "32", "removed": "31", "modified": "33", "type": "35",
          "moved": "36", "duplicate": "35"}


@dataclass
class TreeNode:
    name: str
    path: str
    children: dict[str, "TreeNode"] = field(default_factory=dict)
    change: Change | None = None
    counts: Counter = field(default_factory=Counter)
    steps: list[str] = field(default_factory=list)   # history mode: status per step

    @property
    def is_dir(self) -> bool:
        return bool(self.children) or (self.change is not None and self.change.is_dir)


def _root_for(path: str, roots: list[str]) -> str:
    best = ""
    for r in roots:
        r = r.rstrip("/") or "/"
        if path == r or path.startswith(r + "/") or r == "/":
            if len(r) > len(best):
                best = r
    return best or "/"


def build_tree(changes: list[Change], roots: list[str]) -> TreeNode:
    """Tree whose first level is the roots (profile paths). Directory nodes
    carry counts of every change below them."""
    top = TreeNode(name="", path="")
    for c in changes:
        r = _root_for(c.path, roots)
        node = top.children.get(r)
        if node is None:
            node = top.children[r] = TreeNode(name=r, path=r)
        rel = c.path[len(r):].strip("/") if c.path != r else ""
        chain = [node]
        cur = node
        for part in ([x for x in rel.split("/") if x] if rel else []):
            nxt = cur.children.get(part)
            if nxt is None:
                nxt = cur.children[part] = TreeNode(name=part, path=cur.path.rstrip("/") + "/" + part)
            cur = nxt
            chain.append(cur)
        cur.change = c
        # Count files, not directory entries: a collapsed dir move/duplicate
        # carries its file count; a bare dir entry (added/removed dir) is
        # represented by its own node and by the files below it.
        weight = c.files if c.files else (0 if c.is_dir else 1)
        for n in chain:
            n.counts[c.status] += weight
    return top


def summarize(counts: Counter) -> str:
    order = ["added", "removed", "modified", "type", "moved", "duplicate"]
    return " ".join(f"{GLYPH[k]}{counts[k]}" for k in order if counts.get(k))


def render_tree(tree: TreeNode, depth: int = 2, expand: list[str] | None = None,
                show_all: bool = False, color: bool | None = None, max_children: int = 40) -> str:
    """Text rendering. Directories deeper than `depth` collapse to a count;
    directories whose whole subtree is one change (added/removed/moved dir)
    always collapse unless `show_all` or explicitly expanded."""
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    expand = [e.rstrip("/") for e in (expand or [])]
    lines: list[str] = []

    def paint(status: str, text: str) -> str:
        return f"\x1b[{_COLOR[status]}m{text}\x1b[0m" if color else text

    def label(n: TreeNode) -> str:
        if n.change:
            c = n.change
            name = n.name + ("/" if c.is_dir else "")
            extra = ""
            if c.status == "moved":
                extra = f"  -> {c.other}" + (f"  ({c.files} files)" if c.files else "")
            elif c.status == "duplicate":
                extra = f"  identical to {c.other}  ({c.files} files) -- stale copy?"
            elif c.is_dir and c.files == 0 and n.children:
                extra = f"  ({summarize(n.counts)})"
            elif c.is_dir and n.counts:
                extra = f"  ({sum(n.counts.values())} files)"
            elif c.status == "modified" and c.a and c.b and c.a.type == "file":
                extra = f"  {_fmt_size(c.a.size)} -> {_fmt_size(c.b.size)}"
            return paint(c.status, f"{GLYPH[c.status]} {name}") + extra
        return f"  {n.name}/  ({summarize(n.counts)})"

    def walk(n: TreeNode, prefix: str, level: int) -> None:
        kids = sorted(n.children.values(), key=lambda k: (not k.children, k.name))
        wants = show_all or any(n.path == e or n.path.startswith(e + "/") for e in expand)
        collapsed_whole = n.change is not None and n.change.status in ("added", "removed", "moved", "duplicate")
        if not kids or (not wants and (level >= depth or collapsed_whole)):
            if kids and not collapsed_whole:
                lines.append(f"{prefix}    …  ({summarize(n.counts)}; --expand {n.path} or --depth)")
            return
        shown = kids if wants else kids[:max_children]
        for i, k in enumerate(shown):
            last = i == len(shown) - 1 and len(shown) == len(kids)
            branch = "└── " if last else "├── "
            lines.append(f"{prefix}{branch}{label(k)}")
            walk(k, prefix + ("    " if last else "│   "), level + 1)
        if len(shown) < len(kids):
            lines.append(f"{prefix}└── … {len(kids) - len(shown)} more (--expand {n.path})")

    for root in sorted(tree.children.values(), key=lambda k: k.name):
        head = f"{root.path}  ({summarize(root.counts)})" if root.counts else root.path
        if root.change and not root.children:
            head = label(root)
        lines.append(head)
        walk(root, "", 0)
    return "\n".join(lines)


def _fmt_size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}B"


def flat_lines(changes: list[Change]) -> Iterator[str]:
    for c in changes:
        g = GLYPH[c.status]
        if c.status == "moved":
            yield f"{g} {c.path} -> {c.other}"
        elif c.status == "duplicate":
            yield f"{g} {c.path} == {c.other}"
        else:
            yield f"{g} {c.path}"


# -- history across several snapshots ------------------------------------------


def history(listings: list[Listing], detect_moves: bool = True,
            roots: list[str] | None = None) -> tuple[list[list[Change]], TreeNode]:
    """Consecutive diffs across N listings and a merged tree whose nodes carry
    one status glyph per step ('.' = untouched in that step)."""
    steps = [diff_listings(listings[i], listings[i + 1], detect_moves)
             for i in range(len(listings) - 1)]
    merged: dict[str, list[str]] = defaultdict(lambda: ["."] * len(steps))
    last: dict[str, Change] = {}
    for i, chs in enumerate(steps):
        for c in chs:
            merged[c.path][i] = GLYPH[c.status]
            last[c.path] = c
    top = build_tree(list(last.values()), roots=roots or ["/"])

    def attach(n: TreeNode) -> None:
        if n.path in merged:
            n.steps = merged[n.path]
        for k in n.children.values():
            attach(k)
    attach(top)
    return steps, top


def render_history(tree: TreeNode, labels: list[str], depth: int = 3, show_all: bool = False) -> str:
    """Rows of `[glyph per step] path`, indented as a tree."""
    lines = ["steps: " + "  ".join(f"{i + 1}:{lab}" for i, lab in enumerate(labels))]
    width = 2 * len(labels) - 1

    def walk(n: TreeNode, level: int) -> None:
        for k in sorted(n.children.values(), key=lambda x: (not x.children, x.name)):
            strip = " ".join(k.steps) if k.steps else " " * width
            indent = "  " * level
            name = k.name if level else (k.path if k.path != "/" else "")
            if k.children and (level < depth or show_all):
                lines.append(f"[{strip}] {indent}{name}/")
                walk(k, level + 1)
            elif k.children:
                lines.append(f"[{strip}] {indent}{name}/  …({summarize(k.counts)})")
            else:
                tail = f" -> {k.change.other}" if k.change and k.change.other else ""
                lines.append(f"[{strip}] {indent}{name}{tail}")
    walk(tree, 0)
    return "\n".join(lines)


# -- convenience for callers ---------------------------------------------------


def roots_for(changes: list[Change], profile_paths: list[str], snapshot_paths: list[str]) -> list[str]:
    """Profile paths that actually cover changes, else the snapshot's own
    paths (sandboxed tests), else '/'."""
    for cand in (profile_paths, snapshot_paths):
        cand = [c.rstrip("/") or "/" for c in cand]
        if cand and all(_root_for(c.path, cand) != "/" for c in changes):
            return cand
    return snapshot_paths or ["/"]


def local_path(root: str, sys_path: str) -> Path:
    return Path(sys_path) if root.rstrip("/") in ("", "/") else Path(root.rstrip("/") + sys_path)
