"""synctool CLI. Thin layer over the library; kickstart, CI, and future
GUIs should call the same library functions these commands do."""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from pathlib import Path

from . import SynctoolError, __version__
from .manifest import MANIFEST_NAME, DriveLayout, find_drive
from .store import ResticStore
from . import sync as syncmod
from . import workspace as ws


def _layout_store(args) -> tuple[DriveLayout, ResticStore]:
    layout = find_drive(args.drive)
    return layout, ResticStore(layout)


# -- commands ----------------------------------------------------------------


def cmd_init_drive(args) -> int:
    root = Path(args.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    layout = DriveLayout(root=root)

    template = Path(__file__).resolve().parents[2] / "drive-template"
    if not layout.manifest_path.exists():
        if template.is_dir():
            shutil.copytree(template, root, dirs_exist_ok=True)
        else:
            layout.manifest_path.write_text(_FALLBACK_MANIFEST)
    layout.workspace_root.mkdir(parents=True, exist_ok=True)

    if not layout.password_file.exists():
        pw = args.password or secrets.token_urlsafe(32)
        layout.password_file.write_text(pw + "\n")
        layout.password_file.chmod(0o600)
        if not args.password:
            print(
                "note: generated a random repo password in .restic-pass on the drive.\n"
                "      Anyone holding the drive can read the repo. Copy the password\n"
                "      somewhere safe and delete the file if you want theft protection\n"
                "      (you will then be prompted / must set RESTIC_PASSWORD)."
            )

    store = ResticStore(layout)
    if not (layout.repo / "config").exists():
        store.init()
    print(f"initialized synctool drive at {root}")
    print(f"edit {layout.manifest_path} to define what gets backed up, then run:")
    print(f"  synctool --drive {root} snapshot")
    return 0


def cmd_snapshot(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    snap = syncmod.take_system_snapshot(
        store, layout, manifest, args.profile, root=args.root
    )
    # This snapshot now represents this host's known-good state: record it
    # as the base used by sync's conflict detection.
    state = syncmod.HostState.load(store.repo_id())
    state.base_snapshot = snap
    state.save()
    print(f"created system snapshot {snap[:8]}")
    return 0


def cmd_list(args) -> int:
    layout, store = _layout_store(args)
    snaps = store.snapshots()
    if not snaps:
        print("no snapshots yet")
        return 0
    for s in snaps:
        kind = "commit" if s.is_commit else ("system" if s.is_system else "other")
        extra = ""
        if s.is_commit:
            try:
                extra = "  " + ws.read_commit_meta(store, s).get("message", "")
            except SynctoolError:
                pass
        tags = ",".join(t for t in s.tags if not t.startswith("profile:")) or "-"
        print(f"{s.short_id}  {s.time[:19]}  {kind:7}  {s.hostname:15} {tags}{extra}")
    return 0


def cmd_ls(args) -> int:
    layout, store = _layout_store(args)
    snap = _resolve_snapshot(store, args.snapshot)
    for node in store.ls(snap):
        if node.get("type") == "file":
            print(node["path"])
    return 0


def cmd_checkout(args) -> int:
    layout, store = _layout_store(args)
    from .hooks import run_hooks

    snap = _resolve_snapshot(store, args.snapshot)
    run_hooks(layout, "pre-checkout", {"snapshot": snap})
    files = ws.checkout(store, layout, snap, args.paths)
    for f in files:
        print(f"  {f}")
    print(f"checked out {len(files)} file(s) into {layout.workspace_root}")
    print("edit them there, then: synctool commit -m 'what changed'")
    return 0


def cmd_status(args) -> int:
    layout, _ = _layout_store(args)
    files = ws.status(layout)
    if not files:
        print("workspace empty")
    for f in files:
        print(f"  {f}")
    return 0


def cmd_commit(args) -> int:
    layout, store = _layout_store(args)
    latest_system = [s for s in store.snapshots(tags=["system"])]
    base = latest_system[-1].id if latest_system else None
    snap = ws.commit(store, layout, args.message, base)
    print(f"committed as {snap[:8]}: {args.message}")
    return 0


def cmd_sync(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    result = syncmod.sync(
        store, layout, manifest, root=args.root, force=args.force, dry_run=args.dry_run
    )
    syncmod.print_sync_result(result)
    if result.applied_commits and not args.dry_run and not args.no_snapshot:
        snap = syncmod.take_system_snapshot(store, layout, manifest, None, root=args.root)
        state = syncmod.HostState.load(store.repo_id())
        state.base_snapshot = snap
        state.save()
        print(f"post-sync system snapshot {snap[:8]} recorded as new base")
    return 1 if result.conflicts and not args.force else 0


def cmd_revert(args) -> int:
    layout, store = _layout_store(args)
    safety = syncmod.revert(store, layout, args.snapshot, args.paths or None, root=args.root)
    if safety:
        print(f"pre-revert safety snapshot: {safety[:8]}")
    print(f"reverted to {args.snapshot}")
    return 0


def cmd_restore_full(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    snap = syncmod.restore_full(
        store, layout, manifest, args.profile, root=args.root, snapshot_id=args.snapshot
    )
    print(f"restored system snapshot {snap.short_id} ({snap.time[:19]}) onto {args.root}")
    return 0


def cmd_diff(args) -> int:
    layout, store = _layout_store(args)
    for e in store.diff(args.a, args.b):
        print(f"{e.action}  {e.path}")
    return 0


def cmd_verify(args) -> int:
    layout, store = _layout_store(args)
    store.check()
    print("repository integrity OK")
    return 0


def cmd_prune(args) -> int:
    layout, store = _layout_store(args)
    store.forget(keep_last=args.keep_last)
    print(f"pruned; kept last {args.keep_last} system snapshots (all commits retained)")
    return 0


def cmd_mount(args) -> int:
    layout, store = _layout_store(args)
    print(f"mounting repository at {args.target} (Ctrl-C to unmount)")
    store.mount(Path(args.target))
    return 0


def cmd_browse(args) -> int:
    layout, store = _layout_store(args)
    try:
        from .browse import run_browser
    except ImportError:
        print(
            "the browser needs Textual: pip install 'synctool[tui]'\n"
            "meanwhile, try:  synctool ls latest | fzf  or  synctool mount /tmp/snap",
            file=sys.stderr,
        )
        return 1
    snap = _resolve_snapshot(store, args.snapshot)
    return run_browser(store, layout, snap)


# -- plumbing ----------------------------------------------------------------


def _resolve_snapshot(store, spec: str) -> str:
    if spec != "latest":
        return spec
    snaps = store.snapshots(tags=["system"])
    if not snaps:
        raise SynctoolError("no system snapshots yet; run 'synctool snapshot' first")
    return snaps[-1].id


_FALLBACK_MANIFEST = """\
[synctool]
default_profile = "home"

[profiles.home]
paths = ["/home"]
excludes = ["**/.cache", "**/node_modules", "**/.venv"]
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="synctool",
        description="snapshot/checkout/commit/sync over a restic repo on an external drive",
    )
    p.add_argument("--drive", help=f"drive root (dir containing {MANIFEST_NAME})")
    p.add_argument(
        "--root",
        default="/",
        help="target system root (default /; point at a sandbox dir for testing)",
    )
    p.add_argument("--version", action="version", version=f"synctool {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-drive", help="turn a directory/mounted drive into a synctool drive")
    s.add_argument("path")
    s.add_argument("--password", help="repo password (default: generate and store on drive)")
    s.set_defaults(func=cmd_init_drive)

    s = sub.add_parser("snapshot", help="take a system snapshot of a profile")
    s.add_argument("-p", "--profile")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("list", help="list snapshots (system + commits)")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("ls", help="list files inside a snapshot")
    s.add_argument("snapshot", nargs="?", default="latest")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("checkout", help="extract files from a snapshot into the drive workspace")
    s.add_argument("paths", nargs="+", help="absolute paths (files or dirs) to check out")
    s.add_argument("-s", "--snapshot", default="latest")
    s.set_defaults(func=cmd_checkout)

    s = sub.add_parser("status", help="show files currently in the workspace")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("commit", help="commit workspace edits as a new snapshot")
    s.add_argument("-m", "--message", required=True)
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("sync", help="apply pending commits to this system")
    s.add_argument("--force", action="store_true", help="overwrite conflicting local edits")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("--no-snapshot", action="store_true", help="skip the post-sync base snapshot")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("revert", help="restore paths from an earlier snapshot")
    s.add_argument("snapshot")
    s.add_argument("paths", nargs="*", help="limit to these paths (default: whole snapshot)")
    s.set_defaults(func=cmd_revert)

    s = sub.add_parser("restore-full", help="fresh-system setup from the latest system snapshot")
    s.add_argument("-p", "--profile")
    s.add_argument("-s", "--snapshot", help="specific snapshot id instead of latest")
    s.set_defaults(func=cmd_restore_full)

    s = sub.add_parser("diff", help="diff two snapshots")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("verify", help="check repository integrity")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("prune", help="thin old system snapshots (keeps all commits)")
    s.add_argument("--keep-last", type=int, default=10)
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("mount", help="FUSE-mount the repo for ad-hoc browsing/grep")
    s.add_argument("target")
    s.set_defaults(func=cmd_mount)

    s = sub.add_parser("browse", help="interactive fuzzy search / preview / edit (Textual)")
    s.add_argument("-s", "--snapshot", default="latest")
    s.set_defaults(func=cmd_browse)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SynctoolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
