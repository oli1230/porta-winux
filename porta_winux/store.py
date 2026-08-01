"""SnapshotStore abstraction with a restic backend.

Everything else in porta-winux talks to SnapshotStore, never to restic
directly. Swapping in borg/kopia later means implementing this one class.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import StoreError
from .manifest import DriveLayout


@dataclass
class Snapshot:
    id: str
    short_id: str
    time: str
    hostname: str
    tags: list[str]
    paths: list[str]

    @property
    def is_commit(self) -> bool:
        return "commit" in self.tags

    @property
    def is_system(self) -> bool:
        return "system" in self.tags


@dataclass
class DiffEntry:
    action: str  # "+", "-", "M", "T" (metadata), "U"
    path: str


class SnapshotStore(ABC):
    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def repo_id(self) -> str: ...

    @abstractmethod
    def backup(
        self, paths: list[str], excludes: list[str], tags: list[str], host: str | None = None
    ) -> str:
        """Create a snapshot; return its id."""

    @abstractmethod
    def snapshots(self, tags: list[str] | None = None) -> list[Snapshot]: ...

    @abstractmethod
    def ls(self, snapshot_id: str) -> Iterator[dict]:
        """Yield node dicts: {path, type, size, ...} for a snapshot."""

    @abstractmethod
    def dump(self, snapshot_id: str, path: str) -> bytes:
        """Return file contents of `path` inside a snapshot."""

    @abstractmethod
    def restore(
        self, snapshot_id: str, target: Path, includes: list[str] | None = None
    ) -> None: ...

    @abstractmethod
    def diff(self, a: str, b: str) -> list[DiffEntry]: ...

    @abstractmethod
    def check(self) -> None: ...

    @abstractmethod
    def forget(self, keep_last: int, prune: bool = True) -> None: ...

    @abstractmethod
    def mount(self, target: Path) -> None:
        """Foreground FUSE mount (blocks until unmounted)."""


class ResticStore(SnapshotStore):
    def __init__(self, layout: DriveLayout, restic_bin: str | None = None):
        self.layout = layout
        self.restic = restic_bin or self._find_restic()
        self.env = {
            **os.environ,
            "RESTIC_REPOSITORY": str(layout.repo),
            "RESTIC_PASSWORD_FILE": str(layout.password_file),
        }

    def _find_restic(self) -> str:
        # Prefer the drive's own static binary so a fresh PC needs nothing installed.
        drive_bin = self.layout.bin_dir / "restic"
        if drive_bin.is_file() and os.access(drive_bin, os.X_OK):
            return str(drive_bin)
        found = shutil.which("restic")
        if not found:
            raise StoreError(
                "restic not found on PATH and no static binary at "
                f"{drive_bin}. Install restic or drop a binary on the drive."
            )
        return found

    # -- low-level runner ---------------------------------------------------

    def _run(
        self, *args: str, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess:
        cmd = [self.restic, *args]
        try:
            proc = subprocess.run(
                cmd,
                env=self.env,
                capture_output=capture,
                text=True,
            )
        except FileNotFoundError as e:
            raise StoreError(f"failed to execute {self.restic}: {e}") from e
        if check and proc.returncode != 0:
            err = (proc.stderr or "").strip() if capture else ""
            raise StoreError(f"restic {' '.join(args[:2])} failed (rc={proc.returncode}): {err}")
        return proc

    def _run_json_lines(self, *args: str) -> Iterator[dict]:
        proc = self._run(*args, "--json")
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # restic mixes human lines into some outputs

    # -- API ----------------------------------------------------------------

    def init(self) -> None:
        self._run("init")

    def repo_id(self) -> str:
        proc = self._run("cat", "config")
        return json.loads(proc.stdout)["id"]

    def backup(
        self, paths: list[str], excludes: list[str], tags: list[str], host: str | None = None
    ) -> str:
        args = ["backup", "--json"]
        for t in tags:
            args += ["--tag", t]
        for e in excludes:
            args += ["--exclude", e]
        if host:
            args += ["--host", host]
        args += paths
        proc = self._run(*args)
        snap_id = None
        for line in proc.stdout.splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("message_type") == "summary":
                snap_id = msg.get("snapshot_id")
        if not snap_id:
            raise StoreError("backup succeeded but no snapshot id in restic output")
        return snap_id

    def snapshots(self, tags: list[str] | None = None) -> list[Snapshot]:
        args = ["snapshots"]
        for t in tags or []:
            args += ["--tag", t]
        proc = self._run(*args, "--json")
        out = json.loads(proc.stdout or "[]")
        snaps = [
            Snapshot(
                id=s["id"],
                short_id=s.get("short_id", s["id"][:8]),
                time=s["time"],
                hostname=s.get("hostname", ""),
                tags=s.get("tags") or [],
                paths=s.get("paths") or [],
            )
            for s in out
        ]
        snaps.sort(key=lambda s: s.time)
        return snaps

    def ls(self, snapshot_id: str) -> Iterator[dict]:
        for node in self._run_json_lines("ls", "--recursive", snapshot_id):
            if node.get("struct_type") == "node" or node.get("message_type") == "node":
                yield node

    def dump(self, snapshot_id: str, path: str) -> bytes:
        cmd = [self.restic, "dump", snapshot_id, path]
        proc = subprocess.run(cmd, env=self.env, capture_output=True)
        if proc.returncode != 0:
            raise StoreError(
                f"restic dump {snapshot_id[:8]}:{path} failed: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )
        return proc.stdout

    def restore(self, snapshot_id: str, target: Path, includes: list[str] | None = None) -> None:
        args = ["restore", snapshot_id, "--target", str(target)]
        for inc in includes or []:
            args += ["--include", inc]
        self._run(*args)

    def diff(self, a: str, b: str) -> list[DiffEntry]:
        entries: list[DiffEntry] = []
        for msg in self._run_json_lines("diff", a, b):
            if msg.get("message_type") == "change":
                entries.append(DiffEntry(action=msg["modifier"], path=msg["path"]))
        return entries

    def check(self) -> None:
        self._run("check")

    def forget(self, keep_last: int, prune: bool = True) -> None:
        # Thin system and safety snapshots independently; never touch commits.
        for tag in ("system", "safety"):
            self._run("forget", "--tag", tag, "--keep-last", str(keep_last))
        if prune:
            self._run("prune")

    def mount(self, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        # Foreground: user Ctrl-C's to unmount. Don't capture output.
        subprocess.run([self.restic, "mount", str(target)], env=self.env)
