"""Unit tests for pure logic (no restic needed). Run: pytest"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from porta_winux.manifest import Manifest  # noqa: E402
from porta_winux.sync import _local_path, _sha256  # noqa: E402


def test_manifest_roundtrip(tmp_path):
    m = tmp_path / "porta-winux.toml"
    m.write_text(
        '[porta-winux]\ndefault_profile = "home"\n'
        '[profiles.home]\npaths = ["/home"]\nexcludes = ["**/.cache"]\n'
        '[profiles.etc]\npaths = ["/etc"]\n'
    )
    manifest = Manifest.load(m)
    assert manifest.profile(None).name == "home"
    assert manifest.profile("etc").paths == ["/etc"]


def test_manifest_rejects_relative_paths(tmp_path):
    m = tmp_path / "porta-winux.toml"
    m.write_text('[profiles.bad]\npaths = ["home"]\n')
    import pytest
    from porta_winux import PortaWinuxError
    with pytest.raises(PortaWinuxError):
        Manifest.load(m)


def test_local_path_sandboxing():
    assert _local_path("/", "/etc/hosts") == Path("/etc/hosts")
    assert _local_path("/tmp/sandbox", "/etc/hosts") == Path("/tmp/sandbox/etc/hosts")
    assert _local_path("/tmp/sandbox/", "/etc/hosts") == Path("/tmp/sandbox/etc/hosts")


def test_sha256_stable():
    assert _sha256(b"abc") == _sha256(b"abc")
    assert _sha256(b"abc") != _sha256(b"abd")


def test_fuzzy_filter():
    import pytest
    pytest.importorskip("textual")
    from porta_winux.browse import _fuzzy
    assert _fuzzy("nconf", "/home/user/nginx.conf")
    assert not _fuzzy("xyz", "/home/user/nginx.conf")
    assert _fuzzy("", "anything")


def test_hooks_skip_non_runnables_on_fat_drives(tmp_path):
    """FAT/exFAT drives mark every file executable; placeholders and docs
    must never be executed (regression: Exec format error on .keep)."""
    from porta_winux.hooks import run_hooks
    from porta_winux.manifest import DriveLayout

    layout = DriveLayout(root=tmp_path)
    hd = tmp_path / "hooks.d" / "pre-snapshot"
    hd.mkdir(parents=True)
    # Simulate a FAT drive: everything +x, including junk.
    (hd / ".keep").write_bytes(b"")
    (hd / "README.md").write_text("docs, not a hook")
    (hd / "10-noshebang").write_text("echo missing shebang")
    marker = tmp_path / "ran"
    (hd / "50-real").write_text(f"#!/bin/sh\ntouch {marker}\n")
    for f in hd.iterdir():
        f.chmod(0o755)

    run_hooks(layout, "pre-snapshot", {})  # must not raise
    assert marker.exists()  # the real hook still ran


def test_hooks_env_vars_use_porta_winux_prefix(tmp_path):
    """Regression: runner and shipped hooks must agree on the env prefix."""
    from porta_winux.hooks import run_hooks
    from porta_winux.manifest import DriveLayout

    layout = DriveLayout(root=tmp_path)
    hd = tmp_path / "hooks.d" / "post-sync"
    hd.mkdir(parents=True)
    out = tmp_path / "env.out"
    (hd / "10-dump").write_text(
        f'#!/bin/sh\necho "$PORTA_WINUX_EVENT $PORTA_WINUX_PATHS" > {out}\n'
    )
    (hd / "10-dump").chmod(0o755)
    run_hooks(layout, "post-sync", {"paths": "/a/b"})
    assert out.read_text().strip() == "post-sync /a/b"


def test_is_runnable():
    from porta_winux.hooks import _is_runnable
    from pathlib import Path
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        sh = Path(d) / "s"; sh.write_text("#!/bin/sh\n"); assert _is_runnable(sh)
        elf = Path(d) / "e"; elf.write_bytes(b"\x7fELF junk"); assert _is_runnable(elf)
        txt = Path(d) / "t"; txt.write_text("hello"); assert not _is_runnable(txt)
        empty = Path(d) / "k"; empty.write_bytes(b""); assert not _is_runnable(empty)
