"""Sync engine: bring a live system up to date with commits made on the
drive, revert to earlier states, and perform full restores.

Host state (~/.local/state/synctool/<repo-id>.json):
    base_snapshot   system snapshot representing this host's last known state
    applied         list of commit snapshot ids already applied here

Conflict rule for applying a commit's file F:
    local == incoming            -> already applied, skip
    local missing or local==base -> safe, write
    otherwise                    -> conflict (local changed since base);
                                    skipped unless --force

Every sync/revert takes a safety snapshot of the affected paths first, so
"undo the sync" is always just another revert.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import StoreError, SynctoolError
from .hooks import run_hooks
from .manifest import DriveLayout, Manifest
from .store import Snapshot, SnapshotStore
from .workspace import commit_file_content, read_commit_meta


# -- host state --------------------------------------------------------------


def _state_dir() -> Path:
    if os.environ.get("SYNCTOOL_STATE_DIR"):
        return Path(os.environ["SYNCTOOL_STATE_DIR"])
    xdg = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(xdg) / "synctool"


@dataclass
class HostState:
    repo_id: str
    base_snapshot: str | None = None
    applied: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return _state_dir() / f"{self.repo_id}.json"

    @classmethod
    def load(cls, repo_id: str) -> "HostState":
        p = _state_dir() / f"{repo_id}.json"
        if p.exists():
            data = json.loads(p.read_text())
            return cls(
                repo_id=repo_id,
                base_snapshot=data.get("base_snapshot"),
                applied=data.get("applied", []),
            )
        return cls(repo_id=repo_id)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"base_snapshot": self.base_snapshot, "applied": self.applied}, indent=2
            )
        )


# -- helpers -----------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_path(root: str, abs_path: str) -> Path:
    return Path(root.rstrip("/") + abs_path) if root != "/" else Path(abs_path)


def take_system_snapshot(
    store: SnapshotStore,
    layout: DriveLayout,
    manifest: Manifest,
    profile_name: str | None,
    root: str = "/",
    extra_tags: list[str] | None = None,
) -> str:
    prof = manifest.profile(profile_name)
    paths = [str(_local_path(root, p)) for p in prof.paths]
    missing = [p for p in paths if not Path(p).exists()]
    if len(missing) == len(paths):
        raise SynctoolError(f"none of profile '{prof.name}' paths exist under {root}")
    paths = [p for p in paths if Path(p).exists()]
    run_hooks(layout, "pre-snapshot", {"profile": prof.name}, root=root)
    snap_id = store.backup(
        paths=paths,
        excludes=prof.excludes,
        tags=(["system", f"profile:{prof.name}"] + (extra_tags or [])),
    )
    run_hooks(layout, "post-snapshot", {"profile": prof.name, "snapshot": snap_id}, root=root)
    return snap_id


# -- sync --------------------------------------------------------------------


@dataclass
class SyncResult:
    applied_commits: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    skipped_same: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    safety_snapshot: str | None = None


def pending_commits(store: SnapshotStore, state: HostState) -> list[Snapshot]:
    return [s for s in store.snapshots(tags=["commit"]) if s.id not in state.applied]


def sync(
    store: SnapshotStore,
    layout: DriveLayout,
    manifest: Manifest,
    root: str = "/",
    force: bool = False,
    dry_run: bool = False,
) -> SyncResult:
    state = HostState.load(store.repo_id())
    commits = pending_commits(store, state)
    result = SyncResult()
    if not commits:
        return result

    base_snap: Snapshot | None = None
    if state.base_snapshot:
        base_snap = next(
            (s for s in store.snapshots() if s.id == state.base_snapshot), None
        )

    # Safety net: snapshot the affected paths' current state before touching them.
    all_files: list[tuple[Snapshot, str]] = []
    for c in commits:
        meta = read_commit_meta(store, c)
        for f in meta["files"]:
            all_files.append((c, f))

    if not dry_run:
        existing = sorted(
            {str(_local_path(root, f)) for _, f in all_files if _local_path(root, f).exists()}
        )
        if existing:
            result.safety_snapshot = store.backup(
                paths=existing, excludes=[], tags=["safety", "pre-sync"]
            )

    for commit_snap, abs_path in all_files:
        incoming = commit_file_content(store, commit_snap, abs_path)
        local = _local_path(root, abs_path)
        decision = _decide(local, incoming, base_snap, abs_path, store, force)
        if decision == "same":
            result.skipped_same.append(abs_path)
        elif decision == "conflict":
            result.conflicts.append(abs_path)
        else:  # write
            result.written.append(abs_path)
            if not dry_run:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(incoming)

    clean = not result.conflicts or force
    if not dry_run and clean:
        state.applied.extend(c.id for c in commits)
        state.save()
        result.applied_commits = [c.id for c in commits]
        run_hooks(
            layout,
            "post-sync",
            {"paths": "\n".join(result.written)},
            root=root,
        )
    return result


def _decide(
    local: Path,
    incoming: bytes,
    base_snap: Snapshot | None,
    abs_path: str,
    store: SnapshotStore,
    force: bool,
) -> str:
    if local.exists():
        local_hash = _sha256(local.read_bytes())
        if local_hash == _sha256(incoming):
            return "same"
        if base_snap is not None:
            try:
                base_content = store.dump(base_snap.id, abs_path)
                if _sha256(base_content) == local_hash:
                    return "write"  # unchanged locally since base -> safe fast-forward
            except StoreError:
                pass  # path wasn't in base snapshot; fall through
        return "write" if force else "conflict"
    return "write"  # new file locally


# -- revert & full restore ---------------------------------------------------


def revert(
    store: SnapshotStore,
    layout: DriveLayout,
    snapshot_id: str,
    paths: list[str] | None,
    root: str = "/",
) -> str:
    """Restore paths (default: everything in the snapshot) from an earlier
    system snapshot onto the live system. Returns the safety snapshot id."""
    snaps = {s.id: s for s in store.snapshots()}
    snaps.update({s.short_id: s for s in store.snapshots()})
    if snapshot_id not in snaps:
        raise SynctoolError(f"unknown snapshot {snapshot_id}")
    snap = snaps[snapshot_id]

    affected = paths or snap.paths
    existing = [str(_local_path(root, p)) for p in affected if _local_path(root, p).exists()]
    safety = ""
    if existing:
        safety = store.backup(paths=existing, excludes=[], tags=["safety", "pre-revert"])

    target = Path(root)
    store.restore(snap.id, target=target, includes=paths)
    run_hooks(layout, "post-sync", {"snapshot": snap.id, "paths": "\n".join(affected)}, root=root)
    return safety


def restore_full(
    store: SnapshotStore,
    layout: DriveLayout,
    manifest: Manifest,
    profile_name: str | None,
    root: str = "/",
    snapshot_id: str | None = None,
) -> Snapshot:
    """Fresh-system setup: restore the latest (or given) system snapshot for a
    profile onto `root`, then record it as this host's base."""
    prof = manifest.profile(profile_name)
    candidates = store.snapshots(tags=["system"])
    candidates = [s for s in candidates if f"profile:{prof.name}" in s.tags]
    if snapshot_id:
        candidates = [s for s in candidates if s.id.startswith(snapshot_id)]
    if not candidates:
        raise SynctoolError(f"no system snapshots for profile '{prof.name}'")
    snap = candidates[-1]

    store.restore(snap.id, target=Path(root))
    run_hooks(layout, "post-sync", {"snapshot": snap.id}, root=root)

    state = HostState.load(store.repo_id())
    state.base_snapshot = snap.id
    # A fresh restore already contains every commit merged into snapshots up
    # to `snap`; mark all existing commits applied to avoid re-application.
    state.applied = [c.id for c in store.snapshots(tags=["commit"]) if c.time <= snap.time]
    state.save()
    return snap


def print_sync_result(result: SyncResult) -> None:
    if result.safety_snapshot:
        print(f"safety snapshot: {result.safety_snapshot[:8]}")
    for p in result.written:
        print(f"  updated   {p}")
    for p in result.skipped_same:
        print(f"  unchanged {p}")
    for p in result.conflicts:
        print(f"  CONFLICT  {p}  (local edits since last sync; use --force to overwrite)")
    if not (result.written or result.skipped_same or result.conflicts):
        print("nothing to sync")
    elif result.conflicts:
        print(
            f"\n{len(result.conflicts)} conflict(s); commits left pending. "
            "Resolve locally, snapshot, then re-run sync (or sync --force).",
            file=sys.stderr,
        )


