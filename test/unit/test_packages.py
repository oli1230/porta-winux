"""Unit tests for the packages subsystem. Pure logic + fixtures; no root,
no dnf/flatpak/rpm needed. Run: pytest test/unit -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from porta_winux import PortaWinuxError  # noqa: E402
from porta_winux import packages as pkg  # noqa: E402
from porta_winux.manifest import DriveLayout  # noqa: E402


# -- fixture outputs (captured from real Fedora tools) -------------------------

DNF_USERINSTALLED_QF = "htop\ngit\nrestic\nvlc\n"
DNF_USERINSTALLED_NEVRA = """\
Packages installed by user
htop-3.3.0-3.fc43.x86_64
git-2.47.1-1.fc43.x86_64
code-1.95.3-1.el8.x86_64
"""
DNF_REPOLIST = """\
repo id                          repo name
fedora                           Fedora 43 - x86_64
updates                          Fedora 43 - x86_64 - Updates
rpmfusion-free                   RPM Fusion for Fedora 43 - Free
copr:copr.fedorainfracloud.org:user:proj  Copr repo
code                             Visual Studio Code
"""
FLATPAK_LIST = "org.gimp.GIMP\tflathub\ncom.spotify.Client\tflathub\n"
FLATPAK_REMOTES = "flathub\thttps://dl.flathub.org/repo/\n"
RPM_VA = """\
S.5....T.  c /etc/ssh/sshd_config
.M.......  c /etc/dnf/dnf.conf
..5....T.  c /etc/systemd/logind.conf
missing   c /etc/deleted.conf
S.5....T.  c /etc/NetworkManager/system-connections/home.nmconnection
.......T.  c /etc/only-mtime-changed.conf
S.5....T.    /etc/not-a-config-file
"""


def test_parse_dnf_userinstalled_both_formats():
    assert pkg.parse_dnf_userinstalled(DNF_USERINSTALLED_QF) == ["git", "htop", "restic", "vlc"]
    assert pkg.parse_dnf_userinstalled(DNF_USERINSTALLED_NEVRA) == ["code", "git", "htop"]


def test_parse_repolist_excludes_defaults():
    repos = pkg.parse_dnf_repolist(DNF_REPOLIST)
    assert "fedora" not in repos and "updates" not in repos
    assert repos == ["code", "copr:copr.fedorainfracloud.org:user:proj", "rpmfusion-free"]


def test_parse_flatpak():
    apps = pkg.parse_flatpak_apps(FLATPAK_LIST)
    assert apps[0] == {"id": "com.spotify.Client", "origin": "flathub"}
    assert pkg.parse_flatpak_remotes(FLATPAK_REMOTES) == {
        "flathub": "https://dl.flathub.org/repo/"
    }


def test_parse_rpm_verify_content_changes_only():
    paths = pkg.parse_rpm_verify(RPM_VA)
    assert "/etc/ssh/sshd_config" in paths
    assert "/etc/systemd/logind.conf" in paths
    assert "/etc/dnf/dnf.conf" not in paths          # mode-only change
    assert "/etc/only-mtime-changed.conf" not in paths
    assert "/etc/deleted.conf" not in paths          # missing file
    assert "/etc/not-a-config-file" not in paths     # no 'c' marker


def test_sensitive_flagging():
    assert pkg.is_sensitive("/etc/NetworkManager/system-connections/home.nmconnection")
    assert pkg.is_sensitive("/etc/ssh/ssh_host_ed25519_key")
    assert not pkg.is_sensitive("/etc/ssh/sshd_config")


def test_draft_roundtrip_and_global_list_survival(tmp_path):
    """A scan on machine B must not drop machine A's designations."""
    current = pkg.PackageState(
        dnf_packages=["only-on-desktop", "shared"],
        flatpak_apps=[{"id": "org.desktop.App", "origin": "flathub"}],
        flatpak_remotes={"flathub": "https://x/"},
        fedora_release="43",
    )
    scanned = pkg.PackageState(dnf_packages=["shared", "only-on-laptop"], fedora_release="43")
    draft = pkg.render_packages_draft(current, scanned)
    assert "only-on-desktop" in draft and "not installed on this host" in draft
    assert "only-on-laptop" in draft and "NEW on this host" in draft
    # the draft must be valid TOML and load back to the union
    p = tmp_path / "packages.draft.toml"
    p.write_text(draft)
    loaded = pkg.PackageState.load(p)
    assert set(loaded.dnf_packages) == {"only-on-desktop", "shared", "only-on-laptop"}
    assert loaded.flatpak_apps[0]["id"] == "org.desktop.App"
    assert loaded.fedora_release == "43"


def test_configs_draft_valid_toml_and_sensitive_commented(tmp_path):
    import tomllib
    draft = pkg.render_configs_draft(
        ["/etc/ssh/sshd_config", "/etc/NetworkManager/system-connections/w.nmconnection"],
        hints=[("/home/u/.mozilla", "config for 'firefox'")],
    )
    data = tomllib.loads(draft)["configs"]
    assert data["etc_paths"] == ["/etc/ssh/sshd_config"]   # sensitive one commented out
    assert data["user_paths"] == []                        # hints commented out
    assert "SENSITIVE" in draft and ".mozilla" in draft


def test_adopt_plan_and_execute(tmp_path):
    layout = DriveLayout(root=tmp_path)
    (tmp_path / pkg.PACKAGES_NAME).write_text('[dnf]\npackages = ["old", "shared"]\n')
    (tmp_path / pkg.PACKAGES_DRAFT).write_text('[dnf]\npackages = ["shared", "new"]\n')
    plan = pkg.adopt_plan(layout)
    joined = "\n".join(plan)
    assert "+ new" in joined and "- old" in joined and "never uninstalls" in joined
    done = pkg.adopt_execute(layout)
    assert done == [pkg.PACKAGES_NAME]
    assert not (tmp_path / pkg.PACKAGES_DRAFT).exists()
    assert set(pkg.PackageState.load(tmp_path / pkg.PACKAGES_NAME).dnf_packages) == {"shared", "new"}


def test_adopt_without_drafts_errors(tmp_path):
    with pytest.raises(PortaWinuxError):
        pkg.adopt_execute(DriveLayout(root=tmp_path))


def test_never_remove_guard_blocks_removal_verbs():
    for verb in sorted(pkg.FORBIDDEN_VERBS):
        with pytest.raises(PortaWinuxError):
            pkg._execute(["sudo", "dnf", verb, "-y", "something"], dry_run=True)
    # and normal installs pass (dry-run executes nothing)
    assert pkg._execute(["dnf", "install", "-y", "htop"], dry_run=True) == 0


def test_configs_toml_becomes_profile(tmp_path):
    (tmp_path / "porta-winux.toml").write_text(
        '[porta-winux]\ndefault_profile = "home"\n[profiles.home]\npaths = ["/home"]\n'
    )
    (tmp_path / "configs.toml").write_text(
        '[configs]\netc_paths = ["/etc/ssh/sshd_config"]\nuser_paths = ["/home/u/.mozilla"]\n'
    )
    m = DriveLayout(root=tmp_path).load_manifest()
    prof = m.profile("system-configs")
    assert prof.paths == ["/etc/ssh/sshd_config", "/home/u/.mozilla"]


def test_apply_helpers_report_failures_without_removal(monkeypatch):
    """apply_dnf falls back per-package and reports failures; every command it
    runs is an install."""
    calls = []

    def fake_execute(cmd, dry_run):
        calls.append(cmd)
        assert "install" in cmd and not (set(c.lower() for c in cmd) & pkg.FORBIDDEN_VERBS)
        # bulk attempts fail, per-package: 'bad' fails, others succeed
        if len([c for c in cmd if not c.startswith("-")]) > 4:  # bulk (sudo dnf install x y)
            return 1
        return 1 if "bad" in cmd else 0

    monkeypatch.setattr(pkg, "_execute", fake_execute)
    failed = pkg.apply_dnf(["good1", "bad", "good2"], dry_run=False)
    assert failed == ["bad"]
    assert len(calls) >= 4  # two bulk attempts + per-package


# -- noise-filtering regression tests (the 400-package spin-baseline problem) --

DNF5_STATE = 'version = "1.0"\n[packages]\n' + "\n".join(
    [f'"dep{i}-1.0-1.fc43.x86_64" = {{ reason = "Dependency" }}' for i in range(60)]
    + [f'"grp{i}-1.0-1.fc43.x86_64" = {{ reason = "Group" }}' for i in range(60)]
    + ['"wine-9.0-1.fc43.x86_64" = { reason = "User" }',
       '"bat-0.24-1.fc43.x86_64" = { reason = "User" }']
)

QF_REASON = "\n".join(
    [f"dep{i}\tDependency" for i in range(120)]
    + ["plasma-desktop\tGroup", "wine\tUser", "bat\tUser"]
)


def test_dnf5_state_reason_filter():
    got = pkg.parse_dnf5_state_user(DNF5_STATE.encode())
    assert got == ["bat", "wine"]


def test_dnf5_state_rejects_wrong_shapes():
    assert pkg.parse_dnf5_state_user(b"not toml [") is None
    assert pkg.parse_dnf5_state_user(b'[packages]\n"a-1-1.x" = { reason = "User" }') is None  # too few
    weird = 'version="1"\n[packages]\n' + "\n".join(
        f'"p{i}-1-1.x" = {{ reason = "Banana" }}' for i in range(150))
    assert pkg.parse_dnf5_state_user(weird.encode()) is None  # unknown reasons


def test_qf_reason_parse_and_rejection():
    assert pkg.parse_qf_reason(QF_REASON) == ["bat", "wine"]
    assert pkg.parse_qf_reason("%{name}\t%{reason}\n") is None  # tag unsupported
    assert pkg.parse_qf_reason("a\tUser\n") is None             # too few lines


def test_repolist_header_variants_do_not_leak():
    out = "repository id  repository name\nrepository  something odd\nmullvad-stable  Mullvad\n"
    assert pkg.parse_dnf_repolist(out) == ["mullvad-stable"]


def test_looks_like_base():
    for name in ("plasma-desktop", "iwlwifi-mvm-firmware", "grub2-efi-x64",
                 "kernel-core", "firefox", "spectacle", "NetworkManager-wifi"):
        assert pkg.looks_like_base(name), name
    for name in ("wine", "mullvad-vpn", "librewolf", "bat", "gh", "ansible",
                 "virt-manager", "pass", "f3", "xclip", "tree", "restic"):
        assert not pkg.looks_like_base(name), name


def test_draft_comments_base_but_respects_designations(tmp_path):
    import tomllib
    cur = pkg.PackageState(dnf_packages=["spectacle"])  # user chose it earlier
    scanned = pkg.PackageState(
        dnf_packages=["plasma-desktop", "wine", "spectacle", "firefox"])
    draft = pkg.render_packages_draft(cur, scanned)
    active = tomllib.loads(draft)["dnf"]["packages"]
    assert set(active) == {"wine", "spectacle"}
    assert '# "plasma-desktop"' in draft and '# "firefox"' in draft


def test_baseline_capture_and_load(tmp_path):
    layout = DriveLayout(root=tmp_path)
    pkg.write_baseline(layout, ["plasma-desktop", "firefox"], "test")
    assert pkg.load_baseline(layout) == {"plasma-desktop", "firefox"}
    assert pkg.load_baseline(DriveLayout(root=tmp_path / "empty")) == set()


def test_scan_dnf_prefers_state_file(tmp_path, monkeypatch):
    state = tmp_path / "packages.toml"
    state.write_text(DNF5_STATE)
    monkeypatch.setenv("PORTA_WINUX_DNF5_STATE", str(state))
    names, how = pkg.scan_dnf_packages()
    assert names == ["bat", "wine"] and "reason=User" in how


def test_draft_valid_toml_with_and_without_base_section():
    """Regression: the array must close correctly whether or not any
    base-system candidates exist (the ']' was once eaten by a comment)."""
    import tomllib
    no_base = pkg.render_packages_draft(
        pkg.PackageState(), pkg.PackageState(dnf_packages=["wine", "bat"]))
    assert tomllib.loads(no_base)["dnf"]["packages"] == ["bat", "wine"]
    with_base = pkg.render_packages_draft(
        pkg.PackageState(), pkg.PackageState(dnf_packages=["wine", "plasma-desktop"]))
    assert tomllib.loads(with_base)["dnf"]["packages"] == ["wine"]
