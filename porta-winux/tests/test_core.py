"""Unit tests for pure logic (no restic needed). Run: pytest"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
