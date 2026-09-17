"""Operations: the "why" behind every snapshot.

restic snapshots only carry time, host, paths and tags. That is enough to
find *a* snapshot but not to answer "which one was taken right before I ran
sync --force last Tuesday?". Two additions close that gap:

1. **Operation tags** on every snapshot porta-winux creates:

       op:<6-hex>      the operation that produced it (shared by the safety
                       snapshot and the result snapshot of one command)
       via:<command>   snapshot | commit | sync | restore
       pre:<command>   on safety snapshots: what they were taken before
       forced          the command ran with --force (either meaning: an
                       unclean-session override, or overwriting conflicts)

   Tags travel inside the repo, so they survive copying it anywhere.

2. **The journal** — <drive>/.pw-state/journal.jsonl, append-only, one
   JSON line per event. Every write command appends a `begin` line before
   it touches anything and an `end` line when it finishes (with status,
   the snapshot ids it produced, and a free-text message if the user gave
   one). A `begin` without an `end` is a crash/yank mid-operation.

`porta-winux log` reads the journal; `porta-winux list` joins it onto the
snapshot list, so "find the snapshot from before that --force" is:

    porta-winux log --forced            # each forced op, with its safety snapshot
    porta-winux restore <safety-id>     # undo
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

from . import hostname
from .manifest import DriveLayout
from .store import Snapshot

OP_PREFIX = "op:"
VIA_PREFIX = "via:"
PRE_PREFIX = "pre:"
FORCED_TAG = "forced"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_op_id() -> str:
    return secrets.token_hex(3)


class Operation:
    """Context manager wrapping one write command.

        with Operation(layout, "restore", force=False, args={...}) as op:
            store.backup(..., tags=op.safety_tags())
            store.restore(...)
            op.record(result=snap_id)

    Exceptions propagate unchanged but are recorded in the journal first.
    """

    def __init__(
        self,
        layout: DriveLayout,
        cmd: str,
        *,
        force: bool = False,
        force_reason: str | None = None,
        args: dict[str, Any] | None = None,
        root: str = "/",
        message: str | None = None,
    ):
        self.layout = layout
        self.cmd = cmd
        self.id = new_op_id()
        self.force = force
        self.force_reason = force_reason
        self.args = args or {}
        self.root = root
        self.message = message
        self.snapshots: dict[str, Any] = {}
        self.notes: list[str] = []

    # -- tags ------------------------------------------------------------------

    def tags(self) -> list[str]:
        t = [f"{OP_PREFIX}{self.id}", f"{VIA_PREFIX}{self.cmd}"]
        if self.force:
            t.append(FORCED_TAG)
        return t

    def safety_tags(self) -> list[str]:
        return ["safety", f"{PRE_PREFIX}{self.cmd}"] + self.tags()

    # -- journal ---------------------------------------------------------------

    def record(self, **snapshots: Any) -> None:
        """Remember snapshot ids produced by this op (safety=..., result=...)."""
        self.snapshots.update({k: v for k, v in snapshots.items() if v})

    def note(self, text: str) -> None:
        self.notes.append(text)

    def _append(self, entry: dict[str, Any]) -> None:
        try:
            self.layout.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.layout.journal_path, "a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass  # the journal is a convenience; never let it block the operation

    def __enter__(self) -> "Operation":
        self._append({
            "event": "begin", "op": self.id, "cmd": self.cmd, "time": _now(),
            "host": hostname(), "user": _user(), "root": self.root,
            "forced": self.force, "force_reason": self.force_reason,
            "args": self.args, "message": self.message,
        })
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._append({
            "event": "end", "op": self.id, "cmd": self.cmd, "time": _now(),
            "status": "ok" if exc is None else "error",
            "error": None if exc is None else f"{exc_type.__name__}: {exc}",
            "snapshots": self.snapshots, "notes": self.notes,
        })
        return False


def _user() -> str:
    try:
        return os.environ.get("SUDO_USER") or getpass.getuser()
    except Exception:
        return "?"


# -- reading -------------------------------------------------------------------


def read_journal(layout: DriveLayout) -> list[dict[str, Any]]:
    """Operations, oldest first, with begin and end merged into one record.
    Records missing an `end` get status 'unfinished'."""
    ops: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        lines = layout.journal_path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        oid = e.get("op")
        if not oid:
            continue
        if e.get("event") == "begin":
            rec = dict(e)
            rec.pop("event", None)
            rec["status"] = "unfinished"
            rec["snapshots"] = {}
            rec["notes"] = []
            ops[oid] = rec
            order.append(oid)
        elif e.get("event") == "end" and oid in ops:
            rec = ops[oid]
            rec["status"] = e.get("status", "ok")
            rec["error"] = e.get("error")
            rec["end_time"] = e.get("time")
            rec["snapshots"] = e.get("snapshots") or {}
            rec["notes"] = e.get("notes") or []
    return [ops[o] for o in order]


def op_of(snap: Snapshot) -> str | None:
    return next((t[len(OP_PREFIX):] for t in snap.tags if t.startswith(OP_PREFIX)), None)


def via_of(snap: Snapshot) -> str | None:
    return next((t[len(VIA_PREFIX):] for t in snap.tags if t.startswith(VIA_PREFIX)), None)


def pre_of(snap: Snapshot) -> str | None:
    return next((t[len(PRE_PREFIX):] for t in snap.tags if t.startswith(PRE_PREFIX)), None)


def profile_of(snap: Snapshot) -> str | None:
    return next((t[len("profile:"):] for t in snap.tags if t.startswith("profile:")), None)


def is_forced(snap: Snapshot) -> bool:
    return FORCED_TAG in snap.tags


def kind_of(snap: Snapshot) -> str:
    if snap.is_commit:
        return "commit"
    if "safety" in snap.tags:
        return "safety"
    if snap.is_system:
        return "system"
    return "other"


def annotate(snaps: Iterable[Snapshot], journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join snapshots with journal records. Returns one row per snapshot:
    {snap, kind, op, via, pre, forced, profile, message, undo}.

    `undo` is filled for snapshots produced by a forced op: it names the
    safety snapshot (if any) that captures the state right before it.
    """
    by_op = {rec["op"]: rec for rec in journal}
    rows = []
    for s in snaps:
        oid = op_of(s)
        rec = by_op.get(oid) if oid else None
        row = {
            "snap": s, "kind": kind_of(s), "op": oid, "via": via_of(s), "pre": pre_of(s),
            "forced": is_forced(s) or bool(rec and rec.get("forced")),
            "profile": profile_of(s), "message": (rec or {}).get("message"),
            "status": (rec or {}).get("status"), "undo": None,
        }
        if row["forced"] and rec and row["kind"] != "safety":
            row["undo"] = (rec.get("snapshots") or {}).get("safety")
        rows.append(row)
    return rows


def format_journal_line(rec: dict[str, Any], width_cmd: int = 8) -> str:
    """One-line human rendering of a journal record."""
    t = (rec.get("time") or "")[:19].replace("T", " ")
    flags = []
    if rec.get("forced"):
        flags.append("FORCED" + (f"({rec.get('force_reason')})" if rec.get("force_reason") else ""))
    if rec.get("status") == "error":
        flags.append("ERROR")
    elif rec.get("status") == "unfinished":
        flags.append("UNFINISHED")
    snaps = rec.get("snapshots") or {}
    parts = []
    for k in ("safety", "result", "base"):
        if snaps.get(k):
            parts.append(f"{k}={str(snaps[k])[:8]}")
    a = rec.get("args") or {}
    detail = " ".join(f"{k}={v}" for k, v in a.items() if v not in (None, False, [], ""))
    line = f"{rec['op']}  {t}  {rec['cmd']:<{width_cmd}} {rec.get('host', ''):<12} " \
           f"{' '.join(flags):<12} {' '.join(parts)}"
    if detail:
        line += f"  [{detail}]"
    if rec.get("message"):
        line += f'  "{rec["message"]}"'
    return line.rstrip()
