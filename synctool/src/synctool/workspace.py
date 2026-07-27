"""Workspace: the editable area on the drive.

checkout: extract files from a snapshot into workspace/root/<abs-path>
commit:   snapshot the workspace as a 'commit' snapshot with metadata,
          then clear it (like git: committed changes leave the index)

Commit metadata (message, base snapshot, file list) travels inside the
commit snapshot itself as /.synctool-commit.json, so it needs no side
storage and survives repo copies.
"""

from __future__ import annotations

import json
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from . import SynctoolError
from .manifest import DriveLayout
from .store import Snapshot, SnapshotStore

COMMIT_META = ".synctool-commit.json"


def _workspace_files(layout: DriveLayout) -> list[str]:
    """Absolute system paths currently present in the workspace."""
    ws = layout.workspace_root
    if not ws.is_dir():
        return []
    out = []
    for p in sorted(ws.rglob("*")):
        if p.is_file() and p.name != COMMIT_META:
            out.append("/" + str(p.relative_to(ws)))
    return out


def checkout(
    store: SnapshotStore, layout: DriveLayout, snapshot_id: str, paths: list[str]
) -> list[str]:
    """Extract `paths` (files or directories) from a snapshot into the workspace."""
    ws = layout.workspace_root
    ws.mkdir(parents=True, exist_ok=True)
    # restic restore recreates absolute paths under --target, which is
    # exactly our workspace mirror convention.
    store.restore(snapshot_id, target=ws, includes=paths)
    got = _workspace_files(layout)
    if not got:
        raise SynctoolError(
            "checkout matched no files; check the path spelling against 'synctool ls'"
        )
    return got


def status(layout: DriveLayout) -> list[str]:
    return _workspace_files(layout)


def commit(
    store: SnapshotStore, layout: DriveLayout, message: str, base_snapshot: str | None
) -> str:
    files = _workspace_files(layout)
    if not files:
        raise SynctoolError("workspace is empty; nothing to commit")
    meta = {
        "message": message,
        "base_snapshot": base_snapshot,
        "files": files,
        "created": datetime.now(timezone.utc).isoformat(),
        "committer_host": socket.gethostname(),
    }
    meta_path = layout.workspace_root / COMMIT_META
    meta_path.write_text(json.dumps(meta, indent=2))
    snap_id = store.backup(
        paths=[str(layout.workspace_root)],
        excludes=[],
        tags=["commit"],
        host="synctool-workspace",  # stable host => stable parent chain & dedup
    )
    # Clear the workspace: the commit is now the source of truth.
    shutil.rmtree(layout.workspace_root)
    layout.workspace_root.mkdir(parents=True)
    return snap_id


def read_commit_meta(store: SnapshotStore, snap: Snapshot) -> dict:
    """Read /.synctool-commit.json out of a commit snapshot.

    The drive may be mounted at a different path on each machine, so the
    workspace prefix inside the snapshot is taken from the snapshot's own
    recorded path list, never from the current mount point.
    """
    prefix = _snapshot_prefix(snap)
    try:
        return json.loads(store.dump(snap.id, f"{prefix}/{COMMIT_META}"))
    except Exception as e:
        raise SynctoolError(
            f"snapshot {snap.short_id} has no readable commit metadata: {e}"
        ) from e


def commit_file_content(store: SnapshotStore, snap: Snapshot, abs_path: str) -> bytes:
    """Content of a committed file, addressed by its system absolute path."""
    return store.dump(snap.id, _snapshot_prefix(snap) + abs_path)


def _snapshot_prefix(snap: Snapshot) -> str:
    if not snap.paths:
        raise SynctoolError(f"commit snapshot {snap.short_id} records no paths")
    return snap.paths[0].rstrip("/")
