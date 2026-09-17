"""The `init` command group: everything about turning hardware into a
porta-winux drive and taking it apart again.

    init format <dev>   DESTRUCTIVE partitioning (drivehealth.format_drive)
    init drive [path]   furnish a mounted partition: manifest, signature,
                        restic repo, workspace, hooks
    init detect         read-only: what porta-winux drives are plugged in,
                        and their state -- safe to run any time ("soft launch")
    init cleanup        non-destructive tidy: host state for this drive,
                        stale restic locks, leftover draft files
    init uninstall      DESTRUCTIVE: remove porta-winux from the drive and/or
                        this host (your curation .toml files are kept unless
                        --purge)

The signature file `.porta-winux` (JSON) is what `detect` and every other
command look for first. Drives made before 0.4 have only the manifest;
`init drive` on such a drive adds the signature and touches nothing else.
"""

from __future__ import annotations

import getpass
import os
import secrets
import shutil
import subprocess as sp
import sys
from pathlib import Path

from . import PortaWinuxError, __version__, drivehealth, hostname, interact
from .manifest import MANIFEST_NAME, SIGNATURE_NAME, DriveLayout, scan_drives
from .store import ResticStore

# Files/dirs init-drive creates; uninstall removes exactly these.
DRIVE_ARTIFACTS = ["repo", "workspace", "hooks.d", "bin", ".pw-state", ".restic-pass",
                   MANIFEST_NAME, SIGNATURE_NAME, "bootstrap.sh", "README.md"]
# User curation files that are the *point* of the drive; kept unless --purge.
CURATION_FILES = ["packages.toml", "configs.toml", "baseline.toml"]
DRAFT_SUFFIX = ".draft.toml"

_FALLBACK_MANIFEST = """\
[porta-winux]
default_profile = "home"

[profiles.home]
paths = ["/home"]
excludes = ["**/.cache", "**/node_modules", "**/.venv"]
"""


# -- init drive ---------------------------------------------------------------


def _ensure_writable_root(root: Path, assume_yes: bool) -> None:
    """Fresh btrfs/ext4 filesystems are root-owned; without this, init-drive
    dies in Permission denied. Detect it and offer the chown."""
    if not root.exists() or os.access(root, os.W_OK):
        return
    user = getpass.getuser()
    print(
        f"{root} exists but you can't write to it — freshly formatted Linux\n"
        f"filesystems are owned by root until ownership is handed over."
    )
    interact.confirm(
        f"Run 'sudo chown {user}:{user} {root}' to take ownership?", assume_yes=assume_yes
    )
    if sp.run(["sudo", "chown", f"{user}:{user}", str(root)]).returncode != 0:
        raise PortaWinuxError(f"chown failed; fix ownership of {root} manually and re-run")
    if not os.access(root, os.W_OK):
        raise PortaWinuxError(f"{root} is still not writable after chown")


def init_drive(path: str | None, password: str | None, assume_yes: bool) -> DriveLayout:
    # 1. Where? Prefer an interactive pick over a typed path a human can get wrong.
    if path:
        root = Path(path).resolve()
    else:
        cands = interact.removable_mount_candidates()
        chosen = interact.choose(
            "Which mounted drive should become the porta-winux drive?",
            cands,
            allow_other="type a path manually",
        )
        root = Path(chosen).resolve()

    _ensure_writable_root(root, assume_yes=assume_yes)

    # 2. Show exactly what will happen before anything is written.
    layout = DriveLayout(root=root)
    manifest_preexisting = layout.manifest_path.exists()
    print(f"\nPlan for {root}:")
    if manifest_preexisting:
        print(f"  - {MANIFEST_NAME} already present: existing files will NOT be overwritten")
    else:
        print(f"  - copy drive template: {MANIFEST_NAME}, hooks.d/, bootstrap.sh, bin/")
    if layout.signature_path.exists():
        print(f"  - {SIGNATURE_NAME} signature already present: kept (drive id unchanged)")
    else:
        print(f"  - write {SIGNATURE_NAME} signature (drive id, creation time, version)")
    print("  - create workspace/root/ (editable checkout area)")
    if layout.password_file.exists():
        print("  - .restic-pass already present: kept as-is")
    elif password:
        print("  - write your provided password to .restic-pass (mode 600)")
    else:
        print("  - generate a random repo password into .restic-pass (mode 600)")
    if (layout.repo / "config").exists():
        print("  - repo/ already initialized: left untouched")
    else:
        print("  - initialize an empty restic repository in repo/")
    print("  - nothing outside this directory is touched\n")
    interact.confirm(f"Initialize porta-winux drive at {root}?", assume_yes=assume_yes)

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
    layout.write_signature(__version__)

    if not layout.password_file.exists():
        pw = password or secrets.token_urlsafe(32)
        layout.password_file.write_text(pw + "\n")
        layout.password_file.chmod(0o600)
        if not password:
            print(
                "note: generated a random repo password in .restic-pass on the drive.\n"
                "      Anyone holding the drive can read the repo. Copy the password\n"
                "      somewhere safe and delete the file if you want theft protection\n"
                "      (you will then be prompted / must set RESTIC_PASSWORD)."
            )

    if not manifest_preexisting:
        pinned = drivehealth.pin_fstype(layout)
        if pinned != "unknown":
            print(f"pinned drive filesystem: {pinned} (writes refused if it ever differs)")
    store = ResticStore(layout)
    if not (layout.repo / "config").exists():
        store.init()
    drivehealth.mark_clean(layout)
    print(f"\ninitialized porta-winux drive at {root}")
    print("next step — tell porta-winux WHAT to back up:")
    print(f"  1. edit {layout.manifest_path}  (add your paths under [profiles.*])")
    print(f"  2. porta-winux --drive {root} snapshot")
    return layout


# -- detect ---------------------------------------------------------------------


def describe_drive(layout: DriveLayout, with_repo: bool = True) -> list[str]:
    """Human lines about one drive. Read-only. Never raises."""
    lines: list[str] = []
    sig = layout.read_signature()
    fstype, target, source = drivehealth.fs_info(layout.root)
    pinned = drivehealth.pinned_fstype(layout)
    state = drivehealth.session_state(layout)

    lines.append(f"{layout.root}")
    if sig:
        lines.append(f"  signature   drive {sig.get('drive_id', '?')[:8]}…  created "
                     f"{str(sig.get('created', '?'))[:10]} on {sig.get('created_by', '?')} "
                     f"with {sig.get('created_with', '?')}")
    else:
        lines.append(f"  signature   MISSING (pre-0.4 drive) — 'porta-winux init drive "
                     f"{layout.root}' adds it, changing nothing else")
    lines.append(f"  manifest    {'present' if layout.manifest_path.exists() else 'MISSING'}"
                 + ("" if layout.manifest_path.exists() else " — commands will fail"))
    fs_note = f"{fstype} on {source or '?'} ({target or '?'})"
    if pinned and fstype not in ("unknown", pinned):
        fs_note += f"  MISMATCH: repo expects {pinned} — writes are refused"
    elif pinned:
        fs_note += f"  (pinned: {pinned})"
    lines.append(f"  filesystem  {fs_note}")
    lines.append(f"  session     {state}" + (
        "  — run 'porta-winux verify' before writing" if state == "dirty" else ""))
    lines.append(f"  repo        {'present' if (layout.repo / 'config').exists() else 'MISSING'}"
                 f"{'' if layout.password_file.exists() else ' (no .restic-pass: password will be prompted)'}")
    if with_repo and (layout.repo / "config").exists():
        try:
            store = ResticStore(layout)
            snaps = store.snapshots()
            from . import ops
            kinds = {}
            for s in snaps:
                kinds[ops.kind_of(s)] = kinds.get(ops.kind_of(s), 0) + 1
            hosts = sorted({s.hostname for s in snaps if not s.is_commit})
            last = snaps[-1].time[:19] if snaps else "never"
            lines.append(f"  snapshots   {len(snaps)} ("
                         + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
                         + f"); hosts: {', '.join(hosts) or '-'}; last: {last}")
            from .sync import HostState
            st = HostState.load(store.repo_id())
            base = st.base_snapshot[:8] if st.base_snapshot else "none"
            lines.append(f"  this host   {hostname()}: base snapshot {base}, "
                         f"{len(st.applied)} commit(s) applied  ({st.path})")
        except Exception as e:  # detect must never fail
            lines.append(f"  snapshots   (unavailable: {str(e).splitlines()[0][:80]})")
    from . import ops
    journal = ops.read_journal(layout)
    if journal:
        last_op = journal[-1]
        lines.append(f"  last op     {ops.format_journal_line(last_op)}")
        unfinished = [r for r in journal if r.get("status") == "unfinished"]
        if unfinished:
            lines.append(f"  WARNING     {len(unfinished)} operation(s) never finished "
                         f"(crash/yank): porta-winux log")
    drafts = sorted(p.name for p in layout.root.glob(f"*{DRAFT_SUFFIX}"))
    if drafts:
        lines.append(f"  drafts      {', '.join(drafts)}  (pkg adopt, or init cleanup)")
    return lines


def detect(explicit: str | None, with_repo: bool = True) -> int:
    drives = scan_drives(explicit)
    if not drives:
        from .manifest import drive_candidates
        cands = drive_candidates(explicit)
        print("no porta-winux drive found." + (
            "\nlooked at: " + ", ".join(str(c) for c in cands) if cands else
            "\nnothing is mounted under /run/media/$USER, /media or /mnt"))
        print("  new drive:      porta-winux init format /dev/sdX   then   porta-winux init drive")
        print("  existing drive: porta-winux --drive /path init detect")
        return 1
    for i, d in enumerate(drives):
        if i:
            print()
        for line in describe_drive(d, with_repo=with_repo):
            print(line)
    if len(drives) > 1:
        print(f"\n{len(drives)} drives found; commands use the first unless --drive is given")
    return 0


# -- cleanup -------------------------------------------------------------------


def cleanup_plan(layout: DriveLayout, all_hosts_state: bool = False) -> list[tuple[str, Path | None]]:
    """(description, path) items cleanup would remove. Nothing destructive
    to backups: host state, drafts, stale locks."""
    items: list[tuple[str, Path | None]] = []
    from .sync import _state_dir
    sd = _state_dir()
    if all_hosts_state:
        for p in sorted(sd.glob("*.json")) if sd.is_dir() else []:
            items.append((f"host state {p.name}", p))
    else:
        try:
            rid = ResticStore(layout).repo_id()
            p = sd / f"{rid}.json"
            if p.exists():
                items.append(("host state for this repo (base snapshot + applied commits)", p))
        except Exception:
            pass
    for p in sorted(layout.root.glob(f"*{DRAFT_SUFFIX}")):
        items.append((f"unadopted draft {p.name}", p))
    locks = layout.repo / "locks"
    if locks.is_dir() and any(locks.iterdir()):
        items.append(("stale restic locks (restic unlock)", None))
    return items


def cleanup(layout: DriveLayout, assume_yes: bool, all_hosts_state: bool = False) -> int:
    items = cleanup_plan(layout, all_hosts_state)
    if not items:
        print("nothing to clean up")
        return 0
    print("cleanup plan (backups and curation files are NOT touched):")
    for desc, p in items:
        print(f"  - {desc}" + (f"  [{p}]" if p else ""))
    interact.confirm("Proceed?", assume_yes=assume_yes)
    for desc, p in items:
        if p is None:
            try:
                ResticStore(layout).unlock()
            except Exception as e:
                print(f"  could not unlock: {e}", file=sys.stderr)
                continue
        else:
            p.unlink(missing_ok=True)
        print(f"  removed {desc}")
    return 0


# -- uninstall -------------------------------------------------------------------


def uninstall(layout: DriveLayout | None, drive: bool, host: bool, purge: bool) -> int:
    """Remove porta-winux artifacts. Interactive-only for the drive part:
    the confirmation is typing the drive path (like init format)."""
    from .sync import _state_dir
    sd = _state_dir()
    plan: list[str] = []
    if drive:
        if layout is None:
            raise PortaWinuxError("no drive found to uninstall from (use --host-only, or --drive)")
        present = [n for n in DRIVE_ARTIFACTS if (layout.root / n).exists()]
        plan += [f"drive: remove {layout.root / n}" for n in present]
        if purge:
            plan += [f"drive: remove {layout.root / n}  (curation, --purge)"
                     for n in CURATION_FILES if (layout.root / n).exists()]
        else:
            kept = [n for n in CURATION_FILES if (layout.root / n).exists()]
            if kept:
                plan.append(f"drive: KEEP {', '.join(kept)} (add --purge to remove)")
    if host:
        if sd.is_dir():
            plan += [f"host: remove {p}" for p in sorted(sd.glob('*.json'))]
        plan.append(f"host: remove {sd} if empty")
    plan.append("host: the porta-winux command itself: 'pip uninstall porta-winux' "
                "(and remove the udev rule if you installed setup/99-portawinux.rules)")
    print("uninstall plan:")
    for line in plan:
        print("  - " + line)
    if drive and layout is not None:
        print("\nThis PERMANENTLY DELETES every snapshot in the repo on the drive.")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise PortaWinuxError("init uninstall is interactive-only for the drive part "
                                  "(no --yes bypass); use --host-only non-interactively")
        typed = input(f"Type the drive path ({layout.root}) to confirm: ").strip()
        if typed != str(layout.root):
            raise PortaWinuxError("confirmation did not match; nothing was changed")
        for n in DRIVE_ARTIFACTS + (CURATION_FILES if purge else []):
            p = layout.root / n
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists() or p.is_symlink():
                p.unlink(missing_ok=True)
        print(f"removed porta-winux from {layout.root}")
    if host:
        if sd.is_dir():
            for p in sd.glob("*.json"):
                p.unlink(missing_ok=True)
            try:
                sd.rmdir()
            except OSError:
                pass
        print(f"removed host state under {sd}")
    return 0
