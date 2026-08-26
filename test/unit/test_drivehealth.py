"""Unit tests for the storage-integrity layer. Run: pytest test/unit -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from porta_winux import PortaWinuxError, drivehealth as dh  # noqa: E402
from porta_winux.manifest import DriveLayout  # noqa: E402


def _layout(tmp_path, pinned=None):
    body = '[porta-winux]\ndefault_profile = "h"\n[profiles.h]\npaths = ["/home"]\n'
    if pinned:
        body += f'[storage]\nexpected_fstype = "{pinned}"\n'
    (tmp_path / "porta-winux.toml").write_text(body)
    return DriveLayout(root=tmp_path)


def test_marker_lifecycle(tmp_path):
    lo = _layout(tmp_path)
    assert dh.session_state(lo) == "first-run"
    dh.guard_writes(lo)                      # first run passes, claims
    assert dh.session_state(lo) == "dirty"   # mid-operation = dirty by design
    dh.mark_clean(lo)
    assert dh.session_state(lo) == "clean"
    dh.guard_writes(lo)                      # clean session passes again
    assert dh.session_state(lo) == "dirty"


def test_dirty_session_refuses_writes(tmp_path):
    lo = _layout(tmp_path)
    dh.guard_writes(lo)          # claim, then "crash" (no mark_clean)
    with pytest.raises(PortaWinuxError, match="did not end cleanly"):
        dh.guard_writes(lo)
    dh.guard_writes(lo, force=True)          # explicit override allowed
    dh.mark_clean(lo)
    dh.guard_writes(lo)                      # verified/clean -> allowed


def test_wrong_fstype_refused_fuseblk_lectured(tmp_path, monkeypatch):
    lo = _layout(tmp_path, pinned="btrfs")
    monkeypatch.setattr(dh, "fs_info", lambda p: ("fuseblk", "/run/media/u/X", "/dev/sdb2"))
    with pytest.raises(PortaWinuxError) as e:
        dh.guard_writes(lo)
    assert "REFUSING TO WRITE" in str(e.value) and "ntfs-3g" in str(e.value)


def test_matching_fstype_allowed(tmp_path, monkeypatch):
    lo = _layout(tmp_path, pinned="btrfs")
    monkeypatch.setattr(dh, "fs_info", lambda p: ("btrfs", "/run/media/u/X", "/dev/sdb2"))
    dh.guard_writes(lo)


def test_unpinned_risky_fs_warns_but_allows(tmp_path, monkeypatch, capsys):
    lo = _layout(tmp_path)
    monkeypatch.setattr(dh, "fs_info", lambda p: ("exfat", "/run/media/u/X", "/dev/sdb1"))
    dh.guard_writes(lo)
    assert "history of" in capsys.readouterr().err


def test_pin_and_read_fstype(tmp_path, monkeypatch):
    lo = _layout(tmp_path)
    assert dh.pinned_fstype(lo) is None
    monkeypatch.setattr(dh, "fs_info", lambda p: ("btrfs", "/x", "/dev/sdb2"))
    assert dh.pin_fstype(lo) == "btrfs"
    assert dh.pinned_fstype(lo) == "btrfs"
    # manifest must still parse as valid TOML afterwards
    lo.load_manifest()


def test_verify_packs_detects_flipped_byte(tmp_path):
    import hashlib
    lo = _layout(tmp_path)
    d = tmp_path / "repo" / "data" / "aa"
    d.mkdir(parents=True)
    good = b"good pack contents"
    (d / hashlib.sha256(good).hexdigest()).write_bytes(good)
    bad = b"originally fine"
    p = d / hashlib.sha256(bad).hexdigest()
    p.write_bytes(bad[:-1] + b"X")           # at-rest corruption
    mismatches = dh.verify_packs(lo)
    assert mismatches == [str(p)]


def test_verify_packs_clean_and_empty(tmp_path):
    lo = _layout(tmp_path)
    assert dh.verify_packs(lo) == []         # no data dir yet
    import hashlib
    d = tmp_path / "repo" / "data" / "00"
    d.mkdir(parents=True)
    c = b"x" * 100000
    (d / hashlib.sha256(c).hexdigest()).write_bytes(c)
    assert dh.verify_packs(lo) == []


def test_eject_refuses_non_removable_paths(tmp_path, monkeypatch):
    lo = _layout(tmp_path)
    monkeypatch.setattr(dh, "fs_info", lambda p: ("btrfs", "/", "/dev/sda2"))
    with pytest.raises(PortaWinuxError, match="refusing to eject"):
        dh.eject(lo)
    monkeypatch.setattr(dh, "fs_info", lambda p: ("btrfs", "/opt/weird", "/dev/sdb2"))
    with pytest.raises(PortaWinuxError, match="removable-drive"):
        dh.eject(lo)


def test_format_partition_naming():
    assert dh._partition_path("/dev/sda", 2) == "/dev/sda2"
    assert dh._partition_path("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"
    assert dh._partition_path("/dev/loop7", 1) == "/dev/loop7p1"


def test_mounted_partition_message(monkeypatch):
    """The refusal for a busy disk must name each partition and give the exact
    unmount command (regression: 'has mounted partitions' was unhelpful,
    especially when the user actually passed a partition, not a disk)."""
    import subprocess as sp
    real_run = sp.run

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
        r = R()
        if cmd[:2] == ["lsblk", "-dno"] and cmd[2] == "TYPE":
            r.stdout = "disk\n"
        elif cmd[0] == "lsblk" and any("NAME,MOUNTPOINT" in c for c in cmd):
            r.stdout = "sda\n`-sda1 /run/media/u/OliDrive\n"
        elif cmd[0] == "which":
            return real_run(["true"], capture_output=True)
        return r

    monkeypatch.setattr(dh.Path, "is_block_device", lambda self: True)
    monkeypatch.setattr(dh.subprocess, "run", fake_run)
    with pytest.raises(PortaWinuxError) as e:
        dh.format_drive_checks("/dev/sda")
    msg = str(e.value)
    assert "udisksctl unmount -b /dev/sda1" in msg
    assert "/run/media/u/OliDrive" in msg


def test_partition_argument_redirects_to_disk(monkeypatch):
    import subprocess as sp
    real_run = sp.run

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
        r = R()
        if cmd[:2] == ["lsblk", "-dno"] and cmd[2] == "TYPE":
            r.stdout = "part\n"
        elif cmd[0] == "lsblk" and "PKNAME" in cmd:
            r.stdout = "sda\n"
        elif cmd[0] == "which":
            return real_run(["true"], capture_output=True)
        return r

    monkeypatch.setattr(dh.Path, "is_block_device", lambda self: True)
    monkeypatch.setattr(dh.subprocess, "run", fake_run)
    with pytest.raises(PortaWinuxError) as e:
        dh.format_drive_checks("/dev/sda1")
    assert "PARTITION" in str(e.value) and "/dev/sda" in str(e.value)


def test_init_offers_chown_on_unwritable_root(tmp_path, monkeypatch):
    """Regression: init-drive died with shutil Permission denied on a fresh
    root-owned btrfs partition; it must detect and offer the chown instead."""
    from porta_winux import cli

    calls = []
    monkeypatch.setattr(cli, "os", cli.os)
    monkeypatch.setattr(cli.os, "access", lambda p, m: False)
    import subprocess as sp

    class R:
        returncode = 0
    monkeypatch.setattr(sp, "run", lambda cmd, **kw: calls.append(cmd) or R())
    # after the "chown", writability must be rechecked; make it pass 2nd time
    state = {"n": 0}

    def access2(p, m):
        state["n"] += 1
        return state["n"] > 1
    monkeypatch.setattr(cli.os, "access", access2)

    cli._ensure_writable_root(tmp_path, assume_yes=True)
    assert calls and calls[0][:2] == ["sudo", "chown"]
    assert str(tmp_path) in calls[0][-1]


def test_init_writable_root_is_untouched(tmp_path, monkeypatch):
    from porta_winux import cli
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran a command")))
    cli._ensure_writable_root(tmp_path, assume_yes=True)  # tmp_path is writable
