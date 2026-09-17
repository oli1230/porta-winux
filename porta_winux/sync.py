"""Sync engine: bring a live system up to date with commits made on the
drive, and restore earlier states (partial or whole) onto it.

Host state (~/.local/state/porta-winux/<repo-id>.json):
    base_snapshot   system snapshot representing this host's last known state
    applied         list of commit snapshot ids already applied here

Conflict rule for applying a commit's file F:
    local == incoming            -> already applied, skip
    local missing or local==base -> safe, write
    otherwise                    -> conflict (local changed since base);
                                    skipped unless --force

Every sync/restore takes a safety snapshot of the affected paths first, so
"undo the sync" is always just another restore. Safety and result snapshots
carry the same op:<id> tag (see ops.py), so they can be found together.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import PortaWinuxError, StoreError, hostname
from .hooks import run_hooks
from .manifest import DriveLayout, Manifest
from .store import Snapshot, SnapshotStore
from .workspace import commit_file_content, read_commit_meta

# -- host state --------------------------------------------------------------


def _state_dir() -> Path:
    if os.environ.get("PORTA_WINUX_STATE_DIR"):
        return Path(os.environ["PORTA_WINUX_STATE_DIR"])
    xdg = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(xdg) / "porta-winux"


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
        raise PortaWinuxError(f"none of profile '{prof.name}' paths exist under {root}")
    paths = [p for p in paths if Path(p).exists()]
    run_hooks(layout, "pre-snapshot", {"profile": prof.name}, root=root)
    snap_id = store.backup(
        paths=paths,
        excludes=prof.excludes,
        tags=(["system", f"profile:{prof.name}"] + (extra_tags or [])),
        host=hostname(),
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
    safety_tags: list[str] | None = None,
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
                paths=existing, excludes=[], tags=safety_tags or ["safety", "pre:sync"],
                host=hostname(),
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


# -- restore -----------------------------------------------------------------


def snapshot_by_spec(store: SnapshotStore, spec: str,
                     candidates: list[Snapshot] | None = None) -> Snapshot:
    """Resolve an id / short id / unique prefix to a Snapshot."""
    snaps = candidates if candidates is not None else store.snapshots()
    hits = [s for s in snaps if s.id == spec or s.short_id == spec]
    if not hits:
        hits = [s for s in snaps if s.id.startswith(spec)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise PortaWinuxError(f"unknown snapshot '{spec}' (see: porta-winux list)")
    raise PortaWinuxError(f"snapshot prefix '{spec}' is ambiguous: "
                          + ", ".join(h.short_id for h in hits))


def select_restore_snapshot(
    store: SnapshotStore,
    manifest: Manifest,
    profile_name: str | None,
    spec: str | None = None,
    host: str | None = None,
    any_host: bool = False,
) -> Snapshot:
    """Which snapshot `restore` will use.

      spec given (id/prefix)  -> that snapshot, whatever profile or host
      spec == "latest"        -> newest system snapshot of the profile, any host
      no spec                 -> newest system snapshot of the profile taken
                                 on ANOTHER machine (the cross-machine case);
                                 `host` narrows to one machine, `any_host`
                                 allows this machine's own snapshots
    """
    if spec and spec != "latest":
        return snapshot_by_spec(store, spec)
    prof = manifest.profile(profile_name)
    cands = [s for s in store.snapshots(tags=["system"]) if f"profile:{prof.name}" in s.tags]
    me = hostname()
    if host:
        cands = [s for s in cands if s.hostname == host]
        where = f"from host '{host}'"
    elif spec == "latest" or any_host:
        where = "from any host"
    else:
        cands = [s for s in cands if s.hostname != me]
        where = f"from a host other than this one ({me})"
    if not cands:
        hint = " -- add --any-host to use this machine's own snapshots" \
            if not (host or any_host or spec) else ""
        raise PortaWinuxError(f"no system snapshots for profile '{prof.name}' {where}{hint}")
    return cands[-1]


def restore(
    store: SnapshotStore,
    layout: DriveLayout,
    manifest: Manifest,
    snap: Snapshot,
    paths: list[str] | None = None,
    root: str = "/",
    mirror: bool = False,
    safety: bool = True,
    safety_tags: list[str] | None = None,
) -> str | None:
    """Lay `snap` down onto `root`. With `paths`, only those (a partial
    restore); without, the whole snapshot (a full restore, after which this
    host's base is the snapshot and all older commits count as applied).

    mirror=True also deletes files under the restored paths that are not in
    the snapshot -- the profile's excludes are protected so a mirror never
    removes .cache & co. that were simply never backed up. The delete list is
    computed here (see stale_paths) and executed by us, path by path; we do
    not use restic's --delete (see ResticStore.restore for why).

    Returns (safety snapshot id or None, list of deleted paths)."""
    affected = paths or snap.paths
    existing = [str(_local_path(root, p)) for p in affected if _local_path(root, p).exists()]
    safety_id: str | None = None
    if safety and existing:
        safety_id = store.backup(paths=existing, excludes=[],
                                 tags=safety_tags or ["safety", "pre:restore"], host=hostname())

    stale: list[str] = []
    if mirror:
        stale = stale_paths(store, manifest, snap, affected, root)
    store.restore(snap.id, target=Path(root), includes=paths)
    deleted = _delete_paths(stale, root) if stale else []
    run_hooks(layout, "post-sync", {"snapshot": snap.id, "paths": "\n".join(affected)}, root=root)

    if not paths:
        state = HostState.load(store.repo_id())
        state.base_snapshot = snap.id
        # A full restore already contains every commit merged into snapshots up
        # to `snap`; mark all older commits applied to avoid re-application.
        state.applied = [c.id for c in store.snapshots(tags=["commit"]) if c.time <= snap.time]
        state.save()
    return safety_id, deleted


def profile_excludes(manifest: Manifest, snap: Snapshot) -> list[str]:
    prof_name = next((t[len("profile:"):] for t in snap.tags if t.startswith("profile:")), None)
    if prof_name and prof_name in manifest.profiles:
        return list(manifest.profiles[prof_name].excludes)
    return []


def stale_paths(store: SnapshotStore, manifest: Manifest, snap: Snapshot,
                paths: list[str], root: str = "/") -> list[str]:
    """System paths under `paths` that exist locally but not in `snap`, with
    the profile's excludes protected. This is exactly what --mirror deletes,
    and exactly what the restore preview shows as removed."""
    from . import compare as cmp
    excludes = profile_excludes(manifest, snap)
    live = cmp.listing_from_live(paths, excludes, root=root)
    inside = cmp.listing_from_snapshot(store, snap.id, under=paths)
    return sorted(p for p in live if p not in inside)


def _delete_paths(sys_paths: list[str], root: str) -> list[str]:
    """Remove files/symlinks, then directories bottom-up (only if empty --
    a dir still holding excluded files such as .cache is left alone)."""
    deleted: list[str] = []
    for p in sorted(sys_paths, key=len, reverse=True):
        lp = _local_path(root, p)
        try:
            if lp.is_symlink() or lp.is_file():
                lp.unlink()
                deleted.append(p)
            elif lp.is_dir():
                lp.rmdir()
                deleted.append(p)
        except OSError:
            pass  # non-empty dir (protected contents) or vanished; not fatal
    return deleted


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


