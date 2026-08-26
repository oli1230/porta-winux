"""Drive health: the storage-integrity layer, born from a real incident.

An external drive formatted NTFS silently corrupted 27% of a restic repo's
pack files. Two causes: ntfs-3g (FUSE) mishandling restic's concurrent
write/fsync/rename pattern, and Windows Fast Startup flushing stale cached
clusters over the volume after hibernated resumes. Full write-up: STORAGE.md.

Four mechanisms, wired into every porta-winux write path:

  guard_writes()   refuses to write when the drive's filesystem isn't the one
                   pinned in the manifest ([storage] expected_fstype), with a
                   specific error for fuseblk/ntfs-3g. Also refuses after an
                   unclean session until a verify passes. This is the
                   regression test for the incident — do not weaken it.
  mark_clean()     the clean-marker protocol: every successful write op ends
                   by writing <drive>/.pw-state/clean; every write op begins
                   by consuming it. Marker absent at start = the last session
                   ended without reaching the end (yank, crash, power loss),
                   so writes are refused until `porta-winux verify` passes.
  verify_packs()   a restic pack file's NAME is the SHA-256 of its contents,
                   so hashing every file under repo/data detects at-rest
                   corruption in seconds, without the repo password. This is
                   the check that would have caught the incident on day one.
  eject helpers    sync + marker + unmount + device power-off, ending in an
                   explicit SAFE TO UNPLUG.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import PortaWinuxError
from .manifest import DriveLayout

STATE_DIR = ".pw-state"
CLEAN_MARKER = "clean"

# Filesystems on which a Linux restic repo is known-dangerous.
RISKY_FS = {"fuseblk", "ntfs", "ntfs3", "vfat", "exfat", "msdos"}

FUSEBLK_LECTURE = (
    "'fuseblk' means ntfs-3g, the userspace NTFS driver. That exact\n"
    "combination silently corrupted 27% of this project's pack files once\n"
    "already (see STORAGE.md). Reformat the repo partition to btrfs/ext4\n"
    "with 'porta-winux format-drive', or fix the mount, before writing."
)


# -- filesystem identity -------------------------------------------------------


def fs_info(path: Path) -> tuple[str, str, str]:
    """(fstype, mount_target, source_device) of the filesystem containing
    `path`, via findmnt -T (walks up to the owning mount)."""
    try:
        proc = subprocess.run(
            ["findmnt", "-no", "FSTYPE,TARGET,SOURCE", "-T", str(path)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return ("unknown", "", "")
    parts = proc.stdout.split()
    if proc.returncode != 0 or len(parts) < 3:
        return ("unknown", "", "")
    return (parts[0], parts[1], parts[2])


def pinned_fstype(layout: DriveLayout) -> str | None:
    """[storage] expected_fstype from the drive manifest, if pinned."""
    try:
        with open(layout.manifest_path, "rb") as f:
            return tomllib.load(f).get("storage", {}).get("expected_fstype")
    except (OSError, tomllib.TOMLDecodeError):
        return None


def pin_fstype(layout: DriveLayout) -> str:
    """Append [storage] expected_fstype = <detected> to the manifest.
    Called by init-drive on a freshly created manifest."""
    fstype, _, _ = fs_info(layout.root)
    if fstype == "unknown":
        return fstype
    with open(layout.manifest_path, "a") as f:
        f.write(
            "\n[storage]\n"
            "# Pinned by init-drive. porta-winux refuses to write to the repo if the\n"
            "# drive is ever mounted as anything else (e.g. fuseblk = ntfs-3g, which\n"
            "# corrupted a repo once already -- see STORAGE.md). Update only if you\n"
            "# deliberately reformat the drive.\n"
            f'expected_fstype = "{fstype}"\n'
        )
    return fstype


# -- clean-marker protocol -----------------------------------------------------


def _marker(layout: DriveLayout) -> Path:
    return layout.root / STATE_DIR / CLEAN_MARKER


def session_state(layout: DriveLayout) -> str:
    """'clean' | 'dirty' | 'first-run'"""
    if _marker(layout).exists():
        return "clean"
    if (layout.root / STATE_DIR).exists():
        return "dirty"
    return "first-run"


def mark_dirty(layout: DriveLayout) -> None:
    """Explicitly flag the drive as unverified — used when a verification
    FAILS, so every write path refuses until a later verify passes."""
    _claim(layout)


def mark_clean(layout: DriveLayout) -> None:
    m = _marker(layout)
    m.parent.mkdir(parents=True, exist_ok=True)
    import datetime
    import socket
    m.write_text(f"{datetime.datetime.now().isoformat(timespec='seconds')} {socket.gethostname()}\n")
    _sync(layout.root)


def _claim(layout: DriveLayout) -> None:
    """Consume the marker so a crash between now and mark_clean() reads as
    dirty next time."""
    m = _marker(layout)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.unlink(missing_ok=True)
    _sync(layout.root)


def _sync(path: Path) -> None:
    # sync just this filesystem where supported; full sync as fallback
    if subprocess.run(["sync", "-f", str(path)], capture_output=True).returncode != 0:
        subprocess.run(["sync"], capture_output=True)


# -- the guard -----------------------------------------------------------------


def guard_writes(layout: DriveLayout, force: bool = False) -> None:
    """Called at the start of every operation that writes to the repo or the
    system. Refuses on the wrong filesystem or after an unclean session."""
    fstype, target, _ = fs_info(layout.root)
    pinned = pinned_fstype(layout)

    if pinned and fstype != "unknown" and fstype != pinned:
        msg = (
            f"REFUSING TO WRITE. The drive at {target or layout.root} is mounted as "
            f"'{fstype}' but this repo was created on '{pinned}'."
        )
        if fstype == "fuseblk":
            msg += "\n" + FUSEBLK_LECTURE
        raise PortaWinuxError(msg)
    if not pinned and fstype in RISKY_FS:
        print(
            f"warning: this drive is mounted as '{fstype}', which has a history of\n"
            f"silently corrupting restic repos (see STORAGE.md). Strongly consider\n"
            f"'porta-winux format-drive' (btrfs) for the repo partition.",
            file=sys.stderr,
        )

    state = session_state(layout)
    if state == "dirty" and not force:
        raise PortaWinuxError(
            "the previous session on this drive did not end cleanly (unplugged\n"
            "without eject, crash, or power loss), so its contents are unverified.\n"
            "Run 'porta-winux verify' (add --deep after a raw unplug) first; a\n"
            "passing verify clears this. Use --force only if you accept the risk."
        )
    _claim(layout)


# -- fast pack verification ----------------------------------------------------


def verify_packs(layout: DriveLayout) -> list[str]:
    """Hash every pack file under repo/data and compare to its filename
    (restic names packs by their SHA-256). Returns mismatched paths.
    Needs no repo password; detects at-rest corruption in seconds."""
    data = layout.repo / "data"
    if not data.is_dir():
        return []
    files = [p for p in data.rglob("*") if p.is_file()]

    def check(p: Path) -> str | None:
        h = hashlib.sha256()
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError as e:
            return f"{p} (unreadable: {e})"
        return None if h.hexdigest() == p.name else str(p)

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
        return sorted(r for r in ex.map(check, files) if r)


# -- eject ---------------------------------------------------------------------


def eject(layout: DriveLayout, force: bool = False) -> None:
    """sync -> clean marker -> unmount -> power off -> SAFE TO UNPLUG."""
    fstype, target, source = fs_info(layout.root)
    if target in ("/", "/home", "/usr", "/var", ""):
        raise PortaWinuxError(
            f"refusing to eject: the drive path resolves to mount '{target or '?'}'"
        )
    if not (target.startswith(("/run/media", "/media", "/mnt")) or force):
        raise PortaWinuxError(
            f"'{target}' doesn't look like a removable-drive mount point; "
            "use --force if you're sure"
        )
    mark_clean(layout)
    print(f"unmounting {source} ({target})…")
    try:
        proc = subprocess.run(["udisksctl", "unmount", "-b", source], capture_output=True, text=True)
        rc = proc.returncode
    except FileNotFoundError:
        rc = 1
    if rc != 0:
        # fall back to plain umount (needs privileges for non-udisks mounts)
        if subprocess.run(["umount", target]).returncode != 0:
            raise PortaWinuxError(
                f"unmount failed — something is still using {target}. "
                "Close file managers/terminals in it and retry. DO NOT unplug yet."
            )
    # best-effort spin-down/power-off of the parent disk
    parent = subprocess.run(
        ["lsblk", "-no", "PKNAME", source], capture_output=True, text=True
    ).stdout.strip().splitlines()
    if parent:
        try:
            subprocess.run(
                ["udisksctl", "power-off", "-b", f"/dev/{parent[0]}"], capture_output=True
            )
        except FileNotFoundError:
            pass
    print("\n  ==> SAFE TO UNPLUG\n")


# -- destructive drive formatting ---------------------------------------------
# Ported (simplified) from the storage-incident response. Layout:
#   1 PW_SHARED exFAT type 0700 — cross-platform human files, visible everywhere
#   2 PW_LINUX  btrfs type 8300 — the repo; 8300 means Windows assigns no drive
#                                 letter and never offers to "format" it
#   3 PW_WIN    NTFS  type 0700 — reserved for a future Windows repo (optional)


def _partition_path(dev: str, n: int) -> str:
    return f"{dev}p{n}" if dev[-1].isdigit() else f"{dev}{n}"


def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo"]


def format_drive_checks(dev: str) -> list[str]:
    """Safety checks; returns human-readable notes, raises on hard refusals."""
    if not Path(dev).is_block_device():
        raise PortaWinuxError(f"{dev} is not a block device")
    devtype = subprocess.run(
        ["lsblk", "-dno", "TYPE", dev], capture_output=True, text=True
    ).stdout.strip()
    if devtype == "part":
        parent = subprocess.run(
            ["lsblk", "-no", "PKNAME", dev], capture_output=True, text=True
        ).stdout.strip().splitlines()
        parent_dev = f"/dev/{parent[0]}" if parent else "the whole disk"
        raise PortaWinuxError(
            f"{dev} is a PARTITION (the trailing number is the partition index).\n"
            f"format-drive repartitions a WHOLE DISK: use {parent_dev} instead.\n"
            f"Note this erases EVERY partition on {parent_dev}, not just {dev}."
        )
    for c in ("sgdisk", "wipefs", "lsblk", "mkfs.exfat", "mkfs.btrfs", "mkfs.ntfs"):
        if subprocess.run(["which", c], capture_output=True).returncode != 0:
            raise PortaWinuxError(
                f"missing command: {c}  (sudo dnf install gdisk exfatprogs btrfs-progs ntfsprogs)"
            )
    mounted = subprocess.run(
        ["lsblk", "-no", "NAME,MOUNTPOINT", dev], capture_output=True, text=True
    ).stdout
    busy = [ln.split(None, 1) for ln in mounted.splitlines() if len(ln.split(None, 1)) == 2]
    if busy:
        lines = "\n".join(
            f"  /dev/{name.lstrip('`|-')} mounted at {mp}   "
            f"(unmount: udisksctl unmount -b /dev/{name.lstrip('`|-')})"
            for name, mp in busy
        )
        raise PortaWinuxError(
            f"{dev} has mounted partition(s); unmount them first:\n{lines}"
        )
    rootsrc = subprocess.run(
        ["findmnt", "-no", "SOURCE", "/"], capture_output=True, text=True
    ).stdout.strip()
    rootdisk = subprocess.run(
        ["lsblk", "-no", "PKNAME", rootsrc], capture_output=True, text=True
    ).stdout.strip() if rootsrc else ""
    if rootdisk and f"/dev/{rootdisk}" == dev:
        raise PortaWinuxError(f"{dev} is your system disk. Absolutely not.")
    notes = []
    rm = subprocess.run(["lsblk", "-dno", "RM,HOTPLUG", dev], capture_output=True, text=True).stdout.split()
    if rm and rm[0] != "1" and (len(rm) < 2 or rm[1] != "1"):
        notes.append(f"WARNING: {dev} does not look like a removable device")
    return notes


def _reread_partitions(dev: str, dry_run: bool) -> None:
    """Tell the kernel about the new table. partprobe if present, else the
    util-linux fallbacks that every system ships."""
    if dry_run:
        print(f"DRY-RUN: partprobe {dev}")
        return
    for cmd in (["partprobe", dev], ["partx", "-u", dev], ["blockdev", "--rereadpt", dev]):
        try:
            if subprocess.run(_sudo() + cmd, capture_output=True).returncode == 0:
                return
        except FileNotFoundError:
            continue
    print("warning: could not re-read the partition table; unplug/replug the drive", file=sys.stderr)


def format_drive(dev: str, shared: str, linux: str, fstype: str,
                 dup_data: bool, with_win: bool, dry_run: bool = False) -> str:
    """Partition + format. Returns the PW_LINUX partition path.
    Caller is responsible for consent (typed device confirmation)."""
    S = _sudo()

    def run(cmd: list[str]) -> None:
        print(("DRY-RUN: " if dry_run else "running: ") + " ".join(cmd))
        if not dry_run and subprocess.run(cmd).returncode != 0:
            raise PortaWinuxError(f"command failed: {' '.join(cmd)}")

    run(S + ["wipefs", "-a", dev])
    run(S + ["sgdisk", "--zap-all", dev])
    sg = S + ["sgdisk",
              "-n", f"1:0:+{shared}", "-t", "1:0700", "-c", "1:PW_SHARED",
              "-n", f"2:0:{'+' + linux if with_win else '0'}", "-t", "2:8300", "-c", "2:PW_LINUX"]
    if with_win:
        sg += ["-n", "3:0:0", "-t", "3:0700", "-c", "3:PW_WIN"]
    run(sg + [dev])
    _reread_partitions(dev, dry_run)
    if not dry_run:
        import time
        time.sleep(2)
    p1, p2 = _partition_path(dev, 1), _partition_path(dev, 2)
    run(S + ["mkfs.exfat", "-F", "-L", "PW_SHARED", p1])
    if fstype == "btrfs":
        data_profile = ["-d", "dup"] if dup_data else []
        run(S + ["mkfs.btrfs", "-f", "-L", "PW_LINUX", "-m", "dup", *data_profile, p2])
    else:
        run(S + ["mkfs.ext4", "-q", "-F", "-L", "PW_LINUX", p2])
    if with_win:
        run(S + ["mkfs.ntfs", "-Q", "-L", "PW_WIN", _partition_path(dev, 3)])
    _chown_fs_root(p2, dry_run)
    return p2


def _chown_fs_root(part: str, dry_run: bool) -> None:
    """A fresh btrfs/ext4 root is owned by root, so the desktop user can't
    write to it after automount. Mount briefly and hand ownership over."""
    import getpass
    import tempfile
    user = os.environ.get("SUDO_USER") or getpass.getuser()
    if dry_run:
        print(f"DRY-RUN: mount {part}; chown {user} <root>; umount")
        return
    tmp = tempfile.mkdtemp(prefix="pw-chown.")
    S = _sudo()
    try:
        if subprocess.run(S + ["mount", part, tmp], capture_output=True).returncode != 0:
            raise OSError("mount failed")
        subprocess.run(S + ["chown", f"{user}:{user}", tmp], capture_output=True)
        subprocess.run(S + ["umount", tmp], capture_output=True)
        print(f"handed {part} ownership to {user}")
    except OSError:
        print(
            f"note: could not pre-assign ownership of {part}; after replugging run:\n"
            f"      sudo chown {user}:{user} <its mount point>",
            file=sys.stderr,
        )
    finally:
        subprocess.run(S + ["umount", tmp], capture_output=True)
        try:
            os.rmdir(tmp)
        except OSError:
            pass
