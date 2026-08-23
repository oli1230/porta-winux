"""porta-winux CLI. Thin layer over the library; kickstart, CI, and future
GUIs should call the same library functions these commands do."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
from pathlib import Path

from . import PortaWinuxError, __version__
from . import interact
from .manifest import MANIFEST_NAME, DriveLayout, find_drive
from .store import ResticStore
from . import packages as pkgmod
from . import sync as syncmod
from . import workspace as ws


def _layout_store(args) -> tuple[DriveLayout, ResticStore]:
    layout = find_drive(args.drive)
    return layout, ResticStore(layout)


# -- commands ----------------------------------------------------------------


def cmd_init_drive(args) -> int:
    # 1. Where? Prefer an interactive pick over a typed path a human can get wrong.
    if args.path:
        root = Path(args.path).resolve()
    else:
        cands = interact.removable_mount_candidates()
        chosen = interact.choose(
            "Which mounted drive should become the porta-winux drive?",
            cands,
            allow_other="type a path manually",
        )
        root = Path(chosen).resolve()

    # 2. Show exactly what will happen before anything is written.
    layout = DriveLayout(root=root)
    already = layout.manifest_path.exists()
    print(f"\nPlan for {root}:")
    if already:
        print(f"  - {MANIFEST_NAME} already present: existing files will NOT be overwritten")
    else:
        print(f"  - copy drive template: {MANIFEST_NAME}, hooks.d/, bootstrap.sh, bin/")
    print("  - create workspace/root/ (editable checkout area)")
    if layout.password_file.exists():
        print("  - .restic-pass already present: kept as-is")
    elif args.password:
        print("  - write your provided password to .restic-pass (mode 600)")
    else:
        print("  - generate a random repo password into .restic-pass (mode 600)")
    if (layout.repo / "config").exists():
        print("  - repo/ already initialized: left untouched")
    else:
        print("  - initialize an empty restic repository in repo/")
    print("  - nothing outside this directory is touched\n")
    interact.confirm(f"Initialize porta-winux drive at {root}?", assume_yes=args.yes)

    root.mkdir(parents=True, exist_ok=True)

    # Template ships inside the package so pip-installed copies work too.
    template = Path(__file__).resolve().parent / "drive_template"
    if not layout.manifest_path.exists():
        if template.is_dir():
            shutil.copytree(template, root, dirs_exist_ok=True)
            # Wheel installs strip executable bits; restore them — but only
            # on real programs (shebang/ELF), never READMEs or placeholders.
            from .hooks import _is_runnable

            for exe in [root / "bootstrap.sh", *(root / "hooks.d").rglob("*")]:
                if exe.is_file() and _is_runnable(exe):
                    exe.chmod(exe.stat().st_mode | 0o755)
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
    print(f"\ninitialized porta-winux drive at {root}")
    print("next step — tell porta-winux WHAT to back up:")
    print(f"  1. edit {layout.manifest_path}  (add your paths under [profiles.*])")
    print(f"  2. porta-winux --drive {root} snapshot")
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
            except PortaWinuxError:
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
    print("edit them there, then: porta-winux commit -m 'what changed'")
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

    # Always compute the plan first and show it.
    plan = syncmod.sync(store, layout, manifest, root=args.root, force=args.force, dry_run=True)
    if not (plan.written or plan.conflicts or plan.skipped_same):
        print("nothing to sync")
        return 0
    print(f"sync plan (target root: {args.root}):")
    syncmod.print_sync_result(plan)
    if args.dry_run:
        return 1 if plan.conflicts and not args.force else 0
    if plan.written:
        interact.confirm(
            f"Write {len(plan.written)} file(s) to {args.root} "
            "(a safety snapshot is taken first)?",
            assume_yes=args.yes,
        )

    result = syncmod.sync(
        store, layout, manifest, root=args.root, force=args.force, dry_run=False
    )
    syncmod.print_sync_result(result)
    if result.applied_commits and not args.no_snapshot:
        snap = syncmod.take_system_snapshot(store, layout, manifest, None, root=args.root)
        state = syncmod.HostState.load(store.repo_id())
        state.base_snapshot = snap
        state.save()
        print(f"post-sync system snapshot {snap[:8]} recorded as new base")
    return 1 if result.conflicts and not args.force else 0


def cmd_revert(args) -> int:
    layout, store = _layout_store(args)
    scope = ", ".join(args.paths) if args.paths else "every path in the snapshot"
    print(f"revert plan: restore {scope} from snapshot {args.snapshot} onto {args.root}")
    print("a safety snapshot of the current state is taken first")
    interact.confirm("Proceed with revert?", assume_yes=args.yes)
    safety = syncmod.revert(store, layout, args.snapshot, args.paths or None, root=args.root)
    if safety:
        print(f"pre-revert safety snapshot: {safety[:8]}")
    print(f"reverted to {args.snapshot}")
    return 0


def cmd_restore_full(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    snap = syncmod.select_restore_snapshot(store, manifest, args.profile, args.snapshot)
    print(
        f"restore plan: lay down system snapshot {snap.short_id} "
        f"({snap.time[:19]}, host {snap.hostname}) onto {args.root}"
    )
    print("existing files at those paths WILL be overwritten")
    interact.confirm("Proceed with full restore?", assume_yes=args.yes)
    snap = syncmod.restore_full(
        store, layout, manifest, args.profile, root=args.root, snapshot_id=snap.id
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
    print(f"prune plan: PERMANENTLY delete system/safety snapshots beyond the "
          f"newest {args.keep_last} (commits are never pruned)")
    interact.confirm("Proceed with prune?", assume_yes=args.yes)
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
            "the browser needs Textual: pip install 'porta-winux[tui]'\n"
            "meanwhile, try:  porta-winux ls latest | fzf  or  porta-winux mount /tmp/snap",
            file=sys.stderr,
        )
        return 1
    snap = _resolve_snapshot(store, args.snapshot)
    return run_browser(store, layout, snap)




def cmd_pkg_scan(args) -> int:
    layout = find_drive(args.drive)
    current = pkgmod.PackageState.load(layout.root / pkgmod.PACKAGES_NAME)
    scanned, notes = pkgmod.scan_system()
    for n in notes:
        print(f"note: {n}")

    if args.capture_baseline:
        path = pkgmod.write_baseline(
            layout, scanned.dnf_packages,
            f"host scan on Fedora {scanned.fedora_release or '?'}",
        )
        print(f"captured baseline of {len(scanned.dnf_packages)} packages -> {path}")
        print("future scans (on any machine) subtract these from the draft")

    baseline = pkgmod.load_baseline(layout)
    raw_count = len(scanned.dnf_packages)
    if baseline:
        scanned.dnf_packages = [p for p in scanned.dnf_packages if p not in baseline]
        print(f"baseline subtraction: {raw_count} -> {len(scanned.dnf_packages)} dnf candidates")

    draft = layout.root / pkgmod.PACKAGES_DRAFT
    draft.write_text(pkgmod.render_packages_draft(current, scanned))
    new_dnf = len(set(scanned.dnf_packages) - set(current.dnf_packages))
    new_fp = len({a["id"] for a in scanned.flatpak_apps} - {a["id"] for a in current.flatpak_apps})
    print(f"scanned this host: {len(scanned.dnf_packages)} user-installed dnf packages "
          f"({new_dnf} new vs list), {len(scanned.flatpak_apps)} flatpak apps ({new_fp} new), "
          f"{len(scanned.dnf_repos)} non-default repos")
    print(f"draft written (nothing else touched): {draft}")
    print("review/edit it — delete anything you don't want synced — then: porta-winux pkg adopt")
    return 0


def cmd_pkg_scan_configs(args) -> int:
    layout = find_drive(args.drive)
    print("scanning rpm database for altered /etc config files (may take a minute)…")
    try:
        etc = pkgmod.scan_modified_etc()
    except pkgmod.ScanUnavailable as e:
        raise PortaWinuxError(f"config scan needs rpm: {e}")
    if os.geteuid() != 0:
        print("note: running unprivileged; some root-only files may be missed "
              "(re-run with sudo for a complete scan)")
    state = pkgmod.PackageState.load(layout.root / pkgmod.PACKAGES_NAME)
    hints = pkgmod.user_config_hints(state)
    draft = layout.root / pkgmod.CONFIGS_DRAFT
    draft.write_text(pkgmod.render_configs_draft(etc, hints))
    sens = sum(1 for p in etc if pkgmod.is_sensitive(p))
    print(f"found {len(etc)} altered /etc config file(s) "
          f"({sens} flagged SENSITIVE and left commented out), "
          f"{len(hints)} suggested user config path(s) (all commented out)")
    print(f"draft written: {draft}")
    print("review/edit, then: porta-winux pkg adopt")
    return 0


def cmd_pkg_adopt(args) -> int:
    layout = find_drive(args.drive)
    plan = pkgmod.adopt_plan(layout)
    if not plan:
        print("no drafts on the drive; run 'pkg scan' / 'pkg scan-configs' first")
        return 1
    print("adopt plan (changes the lists on the drive only — installs nothing, removes nothing):")
    for line in plan:
        print("  " + line)
    interact.confirm("Adopt draft(s) into the drive's designation files?", assume_yes=args.yes)
    for name in pkgmod.adopt_execute(layout):
        print(f"adopted {name}")
    if (layout.root / pkgmod.CONFIGS_NAME).exists():
        print("configs are now the 'system-configs' profile; snapshot them with:")
        print("  sudo porta-winux snapshot -p system-configs")
    return 0


def cmd_pkg_diff(args) -> int:
    layout = find_drive(args.drive)
    plan = pkgmod.build_apply_plan(layout)
    pkgmod.print_plan(plan)
    return 0


def cmd_pkg_apply(args) -> int:
    layout = find_drive(args.drive)
    plan = pkgmod.build_apply_plan(layout)
    pkgmod.print_plan(plan)
    if not plan.has_work:
        print("nothing to install — this machine already has everything in the list")
        return 0
    if args.dry_run:
        print("dry run — the exact commands a real apply would run:")
        pkgmod.apply_dnf(plan.dnf_to_install, dry_run=True)
        pkgmod.apply_flatpak(plan.remotes_to_add, plan.flatpak_to_install, dry_run=True)
        print("(nothing was executed)")
        return 0
    failed: list[str] = []
    if plan.dnf_to_install:
        interact.confirm(
            f"Install {len(plan.dnf_to_install)} dnf package(s) via "
            f"'{'sudo ' if os.geteuid() else ''}dnf install' (install only, never removes)?",
            assume_yes=args.yes,
        )
        failed += pkgmod.apply_dnf(plan.dnf_to_install, dry_run=False)
    if plan.remotes_to_add or plan.flatpak_to_install:
        interact.confirm(
            f"Add {len(plan.remotes_to_add)} flatpak remote(s) and install "
            f"{len(plan.flatpak_to_install)} flatpak app(s)?",
            assume_yes=args.yes,
        )
        failed += pkgmod.apply_flatpak(plan.remotes_to_add, plan.flatpak_to_install, dry_run=False)
    if plan.repos_missing:
        print("reminder: these repos are still missing here (set them up, then re-run "
              "'pkg apply'): " + ", ".join(plan.repos_missing))
    if failed:
        print(f"{len(failed)} item(s) failed to install: {', '.join(failed)}")
        print("fix names/repos in packages.toml (or set up the missing repos) and re-run 'pkg apply'")
        return 1
    print("apply complete")
    return 0


# -- plumbing ----------------------------------------------------------------


def _resolve_snapshot(store, spec: str) -> str:
    if spec != "latest":
        return spec
    snaps = store.snapshots(tags=["system"])
    if not snaps:
        raise PortaWinuxError("no system snapshots yet; run 'porta-winux snapshot' first")
    return snaps[-1].id


_FALLBACK_MANIFEST = """\
[porta-winux]
default_profile = "home"

[profiles.home]
paths = ["/home"]
excludes = ["**/.cache", "**/node_modules", "**/.venv"]
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="porta-winux",
        description="snapshot/checkout/commit/sync over a restic repo on an external drive",
    )
    p.add_argument("--drive", help=f"drive root (dir containing {MANIFEST_NAME})")
    p.add_argument(
        "--root",
        default="/",
        help="target system root (default /; point at a sandbox dir for testing)",
    )
    p.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip confirmation prompts (for scripts, kickstart, CI)",
    )
    p.add_argument("--version", action="version", version=f"porta-winux {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-drive", help="turn a directory/mounted drive into a porta-winux drive")
    s.add_argument("path", nargs="?", help="drive root (interactive picker if omitted)")
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

    s = sub.add_parser(
        "pkg",
        help="user-designated programs & configs: scan, adopt, diff, apply (install-only)",
    )
    pkg_sub = s.add_subparsers(dest="pkg_cmd", required=True)
    ps = pkg_sub.add_parser("scan", help="scan this host, write editable packages draft")
    ps.add_argument(
        "--capture-baseline",
        action="store_true",
        help="ALSO record this machine's package set as the fresh-install baseline "
             "(run on a machine you haven't installed anything on yet)",
    )
    ps.set_defaults(func=cmd_pkg_scan)
    ps = pkg_sub.add_parser("scan-configs", help="find altered /etc configs + user config suggestions")
    ps.set_defaults(func=cmd_pkg_scan_configs)
    ps = pkg_sub.add_parser("adopt", help="promote edited draft(s) to the drive's designation files")
    ps.set_defaults(func=cmd_pkg_adopt)
    ps = pkg_sub.add_parser("diff", help="desired list vs this machine (report only)")
    ps.set_defaults(func=cmd_pkg_diff)
    ps = pkg_sub.add_parser("apply", help="install what's missing here (staged, confirmed, NEVER removes)")
    ps.add_argument("-n", "--dry-run", action="store_true")
    ps.set_defaults(func=cmd_pkg_apply)

    s = sub.add_parser("browse", help="interactive fuzzy search / preview / edit (Textual)")
    s.add_argument("-s", "--snapshot", default="latest")
    s.set_defaults(func=cmd_browse)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PortaWinuxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # Downstream pipe (grep -q, head, …) closed early: normal, not an error.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
