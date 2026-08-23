"""System-state subsystem: user-designated programs and altered configs.

Lifecycle (mirrors the file side of porta-winux):

    pkg scan          scan THIS machine for user-installed dnf packages,
                      flatpak apps, and non-default repos; write an
                      annotated, editable draft to <drive>/packages.draft.toml.
                      Read-only: touches nothing but the draft file.
    (you edit the draft: delete lines you don't want synced)
    pkg adopt         show the diff vs the current list, confirm, promote
                      draft(s) to packages.toml / configs.toml.
    pkg diff          desired list vs what's installed on THIS machine.
    pkg apply         install what's missing (dnf/flatpak), after a staged,
                      confirmed plan. NEVER removes anything — see below.
    pkg scan-configs  find /etc config files the user has altered (via rpm
                      verification) plus suggested per-app user config paths;
                      write configs.draft.toml. Adopted configs become the
                      'system-configs' profile for normal restic snapshots.

THE NEVER-REMOVE GUARANTEE
    This module contains no uninstall code path. Packages present locally
    but absent from the list are *reported*, never touched. As defense in
    depth, every command executed by apply passes through _execute(),
    which refuses any command containing a removal verb. The unit tests
    assert this guard works.

GLOBAL LIST SEMANTICS
    One list is shared by all machines. A scan on machine B therefore
    NEVER drops entries that aren't installed on B — it annotates them
    ("in list; not installed on this host") and keeps them, so the desktop's
    designations survive a laptop scan. Only you remove lines, in the draft.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomllib

from . import PortaWinuxError
from .manifest import DriveLayout

PACKAGES_NAME = "packages.toml"
PACKAGES_DRAFT = "packages.draft.toml"
CONFIGS_NAME = "configs.toml"
CONFIGS_DRAFT = "configs.draft.toml"

# Repos every stock Fedora already has; anything else is worth recording.
DEFAULT_REPOS = {
    "fedora", "updates", "fedora-modular", "updates-modular",
    "fedora-cisco-openh264", "updates-testing", "updates-archive",
}

# Removal verbs that must never reach a package manager from this module.
FORBIDDEN_VERBS = {"remove", "erase", "uninstall", "autoremove", "purge"}

# Config paths whose contents commonly embed secrets. Flagged, never auto-added.
SENSITIVE_PATTERNS = (
    "shadow", "gshadow", "/etc/ssh/ssh_host_",
    "NetworkManager/system-connections", "crypttab", "/etc/pki/",
    "sudoers", "/etc/sssd/",
)

# Well-known user-level config locations, suggested (commented out) when the
# matching app is detected. Settings formats differ wildly between programs
# (INI, JSON, sqlite, binary dconf...) — we deliberately treat them all as
# opaque FILES and let restic carry them; see PACKAGES.md for the caveats.
APP_CONFIG_HINTS: dict[str, list[str]] = {
    "firefox": ["~/.mozilla"],
    "code": ["~/.config/Code/User"],
    "vim": ["~/.vimrc", "~/.vim"],
    "neovim": ["~/.config/nvim"],
    "git": ["~/.gitconfig", "~/.config/git"],
    "tmux": ["~/.tmux.conf", "~/.config/tmux"],
    "zsh": ["~/.zshrc"],
    "kitty": ["~/.config/kitty"],
    "alacritty": ["~/.config/alacritty"],
    "thunderbird": ["~/.thunderbird"],
}
# GNOME settings (backgrounds, keybindings, extensions config) live in one
# binary database; carrying it as a file works between same-ish GNOME versions.
ALWAYS_HINTS = ["~/.config/dconf/user"]


# -- running external tools ---------------------------------------------------


def _run(cmd: list[str], ok_rcs: tuple[int, ...] = (0,)) -> str:
    """Run a read-only query command, return stdout. Missing tools and
    unexpected exits raise ScanUnavailable with a friendly message."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise ScanUnavailable(f"{cmd[0]} not found on this system")
    if proc.returncode not in ok_rcs:
        raise ScanUnavailable(
            f"`{' '.join(cmd)}` exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        )
    return proc.stdout


class ScanUnavailable(Exception):
    """A scanner's underlying tool is missing or uncooperative; scan degrades."""


def _execute(cmd: list[str], dry_run: bool) -> int:
    """The single choke point for every state-changing command apply runs.
    Refuses removal verbs no matter how the command was built."""
    lowered = {part.lower() for part in cmd}
    bad = lowered & FORBIDDEN_VERBS
    if bad:
        raise PortaWinuxError(
            f"internal safety guard: refusing to run a command containing "
            f"removal verb(s) {sorted(bad)}: {' '.join(cmd)}"
        )
    print(("DRY-RUN: " if dry_run else "running: ") + " ".join(cmd))
    if dry_run:
        return 0
    return subprocess.run(cmd).returncode


def _sudo_prefix() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo"]


# -- state model ---------------------------------------------------------------


@dataclass
class PackageState:
    dnf_packages: list[str] = field(default_factory=list)
    dnf_repos: list[str] = field(default_factory=list)
    flatpak_apps: list[dict] = field(default_factory=list)  # {"id":…, "origin":…}
    flatpak_remotes: dict[str, str] = field(default_factory=dict)  # name -> url
    fedora_release: str = ""

    @classmethod
    def load(cls, path: Path) -> "PackageState":
        if not path.exists():
            return cls()
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(
            dnf_packages=list(data.get("dnf", {}).get("packages", [])),
            dnf_repos=list(data.get("dnf", {}).get("repos", [])),
            flatpak_apps=[
                {"id": a["id"], "origin": a.get("origin", "")}
                for a in data.get("flatpak", {}).get("apps", [])
            ],
            flatpak_remotes=dict(data.get("flatpak", {}).get("remotes", {})),
            fedora_release=data.get("meta", {}).get("fedora_release", ""),
        )


# -- scanners ------------------------------------------------------------------


def fedora_release() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _nevra_to_name(line: str) -> str:
    """'htop-3.3.0-3.fc43.x86_64' -> 'htop'. Already-bare names pass through."""
    line = line.strip()
    if "." in line:
        line = line.rsplit(".", 1)[0]  # drop arch
    parts = line.rsplit("-", 2)
    if len(parts) == 3 and parts[1][:1].isdigit():
        return parts[0]
    return line.strip()


def parse_dnf_userinstalled(out: str) -> list[str]:
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or " " in line or line.endswith(":"):
            continue  # headers / metadata chatter
        names.add(_nevra_to_name(line))
    return sorted(names)


DNF5_STATE_PATH = "/usr/lib/sysimage/libdnf5/packages.toml"

KNOWN_REASONS = {
    "user", "dependency", "weak-dependency", "weak dependency", "weakdep",
    "group", "external-user", "external user", "clean", "unknown", "none",
}


def parse_dnf5_state_user(toml_bytes: bytes) -> list[str] | None:
    """dnf5 records WHY each package is installed in its system-state file.
    reason == 'User' is a real user request; 'Group' is what Anaconda marks
    the whole spin as. Returns None if the format isn't what we expect."""
    try:
        data = tomllib.loads(toml_bytes.decode("utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, UnicodeError):
        return None

    entries: list[tuple[str, str]] = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                if isinstance(val, dict) and isinstance(val.get("reason"), str):
                    entries.append((key, val["reason"]))
                else:
                    walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    # Sanity: we should be looking at a real package database, and the reason
    # values should look like reasons. Otherwise refuse and let callers fall back.
    if len(entries) < 100:
        return None
    reasons = {r.lower() for _, r in entries}
    if not reasons <= KNOWN_REASONS:
        return None
    return sorted({_nevra_to_name(k) for k, r in entries if r.lower() == "user"})


def parse_qf_reason(out: str) -> list[str] | None:
    """Output of `repoquery --installed --qf '%{name}\t%{reason}'`. Returns
    None when the tool echoed the format string back (tag unsupported)."""
    if "%{reason}" in out:
        return None
    names, total = [], 0
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, reason = line.split("\t", 1)
        total += 1
        if reason.strip().lower() not in KNOWN_REASONS:
            return None
        if reason.strip().lower() == "user":
            names.append(name.strip())
    return sorted(set(names)) if total >= 100 else None


def scan_dnf_packages() -> tuple[list[str], str]:
    """Returns (names, how) where `how` describes which strategy produced the
    list, so the user can see whether real install-reason data was available.

    Strategy order:
      1. dnf5 system-state file (reason == User)   — precise
      2. repoquery --qf reason (reason == User)    — precise
      3. repoquery --userinstalled                 — includes the whole spin
         baseline on Anaconda/live installs; the draft's heuristics and an
         optional baseline.toml then do the shrinking.
    """
    state = Path(os.environ.get("PORTA_WINUX_DNF5_STATE", DNF5_STATE_PATH))
    if state.is_file():
        try:
            got = parse_dnf5_state_user(state.read_bytes())
            if got is not None:
                return got, f"install reasons from {state.name} (reason=User)"
        except OSError:
            pass
    try:
        got = parse_qf_reason(
            _run(["dnf", "repoquery", "--installed", "--qf", "%{name}\t%{reason}\n"])
        )
        if got is not None:
            return got, "install reasons via repoquery (reason=User)"
    except ScanUnavailable:
        pass
    last: ScanUnavailable | None = None
    for cmd in (
        ["dnf", "repoquery", "--userinstalled", "--qf", "%{name}\n"],
        ["dnf", "repoquery", "--userinstalled"],
        ["dnf", "history", "userinstalled"],
    ):
        try:
            return (
                parse_dnf_userinstalled(_run(cmd)),
                "--userinstalled (NOTE: on Anaconda/live installs this includes "
                "the whole spin; use the commented base-system section and/or "
                "'pkg scan --capture-baseline' on a fresh machine)",
            )
        except ScanUnavailable as e:
            last = e
    raise last


def parse_dnf_repolist(out: str) -> list[str]:
    repos = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("repo id", "repository id")):
            continue
        repo_id = line.split()[0]
        if repo_id.lower() in ("repo", "repository", "id"):
            continue  # wrapped/odd header lines
        if repo_id and repo_id not in DEFAULT_REPOS and not repo_id.startswith("*"):
            repos.append(repo_id)
    return sorted(set(repos))


def scan_dnf_repos() -> list[str]:
    return parse_dnf_repolist(_run(["dnf", "repolist", "--enabled"]))


def parse_flatpak_apps(out: str) -> list[dict]:
    apps = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if not parts or "." not in parts[0]:
            continue
        apps.append({"id": parts[0].strip(), "origin": parts[1].strip() if len(parts) > 1 else ""})
    return sorted(apps, key=lambda a: a["id"])


def scan_flatpak_apps() -> list[dict]:
    return parse_flatpak_apps(
        _run(["flatpak", "list", "--app", "--columns=application,origin"])
    )


def parse_flatpak_remotes(out: str) -> dict[str, str]:
    remotes = {}
    for line in out.splitlines():
        if not line.strip() or line.lower().startswith("name"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2 and parts[1].startswith(("http", "oci")):
            remotes[parts[0].strip()] = parts[1].strip()
    return remotes


def scan_flatpak_remotes() -> dict[str, str]:
    return parse_flatpak_remotes(_run(["flatpak", "remotes", "--columns=name,url"]))


def scan_system() -> tuple[PackageState, list[str]]:
    """Scan this machine. Returns (state, notes) — notes describe anything
    that couldn't be scanned so the user isn't silently missing data."""
    notes: list[str] = []
    st = PackageState(fedora_release=fedora_release())
    try:
        st.dnf_packages, how = scan_dnf_packages()
        notes.append(f"dnf packages: {how}")
    except ScanUnavailable as e:
        notes.append(f"dnf packages: skipped ({e})")
    for label, fn, setter in (
        ("dnf repos", scan_dnf_repos, lambda v: setattr(st, "dnf_repos", v)),
        ("flatpak apps", scan_flatpak_apps, lambda v: setattr(st, "flatpak_apps", v)),
        ("flatpak remotes", scan_flatpak_remotes, lambda v: setattr(st, "flatpak_remotes", v)),
    ):
        try:
            setter(fn())
        except ScanUnavailable as e:
            notes.append(f"{label}: skipped ({e})")
    return st, notes


# -- installed-on-this-host sets (for diff/apply) -------------------------------


def installed_rpm_names() -> set[str]:
    return {
        line.strip()
        for line in _run(["rpm", "-qa", "--qf", "%{NAME}\n"]).splitlines()
        if line.strip()
    }


def installed_flatpak_ids() -> set[str]:
    try:
        return {a["id"] for a in scan_flatpak_apps()}
    except ScanUnavailable:
        return set()


def enabled_repo_ids() -> set[str]:
    try:
        return set(parse_dnf_repolist(_run(["dnf", "repolist", "--enabled"]))) | DEFAULT_REPOS
    except ScanUnavailable:
        return set()


# -- altered /etc configs --------------------------------------------------------


_VERIFY_RE = re.compile(r"^([SM5DLUGTP.?]{8,10})\s+c\s+(/etc/\S.*)$")


def parse_rpm_verify(out: str) -> list[str]:
    """Config files under /etc whose CONTENT differs from the package default
    ('5' = digest changed, 'S' = size changed). Mode-only changes are skipped."""
    paths = []
    for line in out.splitlines():
        m = _VERIFY_RE.match(line.strip())
        if m and ("5" in m.group(1) or "S" in m.group(1)):
            paths.append(m.group(2))
    return sorted(set(paths))


def scan_modified_etc() -> list[str]:
    # rpm exits 1 when verification finds differences — that's success for us.
    try:
        out = _run(["rpm", "-Va", "--configfiles"], ok_rcs=(0, 1))
    except ScanUnavailable:
        out = _run(["rpm", "-Va"], ok_rcs=(0, 1))  # slower fallback
    return parse_rpm_verify(out)


def is_sensitive(path: str) -> bool:
    return any(pat in path for pat in SENSITIVE_PATTERNS)


def user_config_hints(state: PackageState) -> list[tuple[str, str]]:
    """(path, reason) suggestions for user-level config paths, based on which
    apps are designated. Flatpak apps keep configs under ~/.var/app/<id>."""
    home = os.path.expanduser("~")
    hints: list[tuple[str, str]] = [(p.replace("~", home), "GNOME settings database (binary; see PACKAGES.md)") for p in ALWAYS_HINTS]
    names = " ".join(state.dnf_packages).lower()
    for key, paths in APP_CONFIG_HINTS.items():
        if key in names:
            hints += [(p.replace("~", home), f"config for '{key}'") for p in paths]
    for app in state.flatpak_apps:
        hints.append((f"{home}/.var/app/{app['id']}", f"flatpak data for {app['id']}"))
    # only suggest paths that exist here
    return [(p, why) for p, why in sorted(set(hints)) if os.path.exists(p)]




# -- baseline (fresh-install package set) --------------------------------------

BASELINE_NAME = "baseline.toml"


def load_baseline(layout: DriveLayout) -> set[str]:
    p = layout.root / BASELINE_NAME
    if not p.exists():
        return set()
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return set(data.get("baseline", {}).get("dnf", []))


def write_baseline(layout: DriveLayout, names: list[str], source_note: str) -> Path:
    p = layout.root / BASELINE_NAME
    body = "\n".join(f'  "{n}",' for n in sorted(set(names)))
    p.write_text(
        "# porta-winux baseline: the package set of a FRESH install of your spin.\n"
        "# Captured with:  porta-winux pkg scan --capture-baseline  (run it on a\n"
        "# machine BEFORE installing anything). Scans subtract these packages so\n"
        "# drafts only show what you added on top of the OS.\n"
        f"# source: {source_note}\n\n"
        f"[baseline]\ndnf = [\n{body}\n]\n"
    )
    return p


# -- "probably came with the spin" heuristics ----------------------------------
# Used ONLY to pre-comment lines in the draft when install-reason data wasn't
# available; nothing is dropped, and uncommenting re-designates. Generous by
# design: false positives cost one uncomment, false negatives cost one delete.

BASE_HINT_PREFIXES = (
    "plasma-", "kde-", "kf6-", "kwin", "sddm", "breeze", "aurorae", "akonadi",
    "kernel", "grub2-", "shim-", "dracut", "anaconda", "livesys", "initial-setup",
    "ibus-", "imsettings", "default-fonts", "gstreamer1-", "libreoffice-",
    "pipewire-", "sssd-", "abrt", "alsa-", "cups", "NetworkManager", "systemd",
    "mesa-", "spice-", "fedora-", "gnupg2", "gpgme", "iwl", "b43-", "hplip",
    "gutenprint", "samba-", "openssh-", "dnf5", "glibc", "grub", "plymouth",
    "selinux-", "policycoreutils", "setroubleshoot", "firewall", "hyperv-",
    "open-vm-tools", "virtualbox-guest", "qemu-guest", "udev-", "udisks2",
    "iscsi-", "nfs-", "cifs-", "ntfs", "xfsprogs", "e2fsprogs", "btrfs-progs",
    "dosfstools", "exfatprogs", "hfsplus-", "lvm2", "mdadm", "cryptsetup",
)
BASE_HINT_SUFFIXES = ("-firmware", "-fwcutter", "-openfwwf")
BASE_HINT_EXACT = {
    # KDE spin default apps (present on any fresh install of the spin)
    "dolphin", "konsole", "okular", "gwenview", "spectacle", "ark", "kcalc",
    "kwrite", "kmail", "kontact", "korganizer", "kaddressbook", "akregator",
    "kleopatra", "kamera", "kamoso", "kcharselect", "kdialog", "kfind",
    "khelpcenter", "kinfocenter", "kmenuedit", "kmines", "kpat", "krdc", "krdp",
    "krfb", "kscreen", "kscreenlocker", "ksshaskpass", "kwalletmanager5",
    "kdebugsettings", "kdnssd", "kdotool", "keditbookmarks", "kjournald",
    "kunifiedpush", "knighttime", "kolourpaint", "kmouth", "kexec-tools",
    "elisa-player", "dragon", "filelight", "skanpage", "neochat", "kdialog",
    "audiocd-kio", "ffmpegthumbs", "colord", "colord-kde", "bluedevil",
    "firefox", "mediawriter", "toolbox", "fpaste", "im-chooser", "orca",
    "brltty", "speech-dispatcher", "xwaylandvideobridge", "qrca",
    # base OS plumbing and cli utilities every install has
    "bash", "bash-completion", "bash-color-prompt", "coreutils", "util-linux",
    "filesystem", "setup", "sudo", "tar", "unzip", "zip", "which", "less",
    "less-color", "file", "hostname", "curl", "rpm", "acl", "attr", "at", "bc",
    "bzip2", "cpio", "crontabs", "ed", "ethtool", "logrotate", "lsof",
    "man-db", "man-pages", "mailcap", "ncurses", "pciutils", "usbutils",
    "rsync", "rsyslog", "smartmontools", "sos", "tcpdump", "traceroute",
    "time", "whois", "words", "wget2-wget", "wcurl", "zram-generator-defaults",
    "mcelog", "microcode_ctl", "irqbalance", "thermald", "efibootmgr",
    "mactel-boot", "memtest86+", "isomd5sum", "deltarpm", "dnsmasq",
    "dhcp-client", "ppp", "realmd", "quota", "lm_sensors", "nvme-cli",
    "lsscsi", "lrzsz", "minicom", "mpage", "paps", "pinfo", "procinfo",
    "procps-ng", "psacct", "symlinks", "usb_modeswitch", "prefixdevname",
    "rootfiles", "passwdqc", "opensc", "hidapi", "capstone", "libpfm",
    "shadow-utils", "chrony", "audit", "authselect", "hunspell", "parted",
    "gdisk", "compsize", "f2fs-tools", "iproute", "iputils", "net-tools",
    "nmap-ncat", "bind-utils", "mtr", "iptstate", "iptables-nft", "teamd",
    "wpa_supplicant", "wireplumber", "dbus", "at-spi2-atk", "at-spi2-core",
    "fuse", "fwupd", "plocate", "mm", "ModemManager", "dns-root-data",
    "nss-mdns", "cyrus-sasl-plain", "openh264", "hostname", "ghostscript",
    "vim-minimal", "default-editor", "dos2unix", "aajohan-comfortaa-fonts",
    "source-foundry-hack-fonts", "desktop-backgrounds-kde", "mm-common",
}


def looks_like_base(name: str) -> bool:
    return (
        name in BASE_HINT_EXACT
        or name.startswith(BASE_HINT_PREFIXES)
        or name.endswith(BASE_HINT_SUFFIXES)
    )


# -- draft rendering -------------------------------------------------------------


def _toml_str_list(items: list[str], annotate: dict[str, str]) -> str:
    lines = []
    for it in items:
        note = annotate.get(it, "")
        lines.append(f'  "{it}",' + (f"  # {note}" if note else ""))
    return "\n".join(lines)


def render_packages_draft(current: PackageState, scanned: PackageState) -> str:
    """Union of current list and this machine's scan, annotated. The user
    edits this file (delete unwanted lines) and then runs `pkg adopt`."""
    all_pkgs = sorted(set(current.dnf_packages) | set(scanned.dnf_packages))
    # Anything already designated stays active regardless of heuristics; the
    # base-system guess only pre-comments NEW candidates.
    base_guess = sorted(
        p for p in all_pkgs if p not in current.dnf_packages and looks_like_base(p)
    )
    pkgs = [p for p in all_pkgs if p not in base_guess]
    pkg_notes = {}
    for p in pkgs:
        if p not in current.dnf_packages:
            pkg_notes[p] = "NEW on this host"
        elif p not in scanned.dnf_packages:
            pkg_notes[p] = "in list; not installed on this host"
    base_block = ""
    if base_guess:
        base_lines = "\n".join(f'  # "{p}",' for p in base_guess)
        base_block = (
            "  # ---- probably part of the Fedora spin / base system ----\n"
            "  # These ship with a fresh install, so syncing them is redundant.\n"
            "  # UNCOMMENT any you deliberately installed and want synced.\n"
            + base_lines + "\n"
        )

    repos = sorted(set(current.dnf_repos) | set(scanned.dnf_repos))
    repo_notes = {r: "NEW" for r in repos if r not in current.dnf_repos}

    cur_ids = {a["id"] for a in current.flatpak_apps}
    scan_ids = {a["id"] for a in scanned.flatpak_apps}
    merged_apps = {a["id"]: a for a in current.flatpak_apps}
    for a in scanned.flatpak_apps:
        merged_apps.setdefault(a["id"], a)
    app_lines = []
    for app_id in sorted(merged_apps):
        a = merged_apps[app_id]
        note = ""
        if app_id not in cur_ids:
            note = "  # NEW on this host"
        elif app_id not in scan_ids:
            note = "  # in list; not installed on this host"
        app_lines.append(f'  {{ id = "{a["id"]}", origin = "{a.get("origin","")}" }},{note}')

    remotes = {**current.flatpak_remotes, **scanned.flatpak_remotes}
    remote_lines = "\n".join(f'{name} = "{url}"' for name, url in sorted(remotes.items()))

    rel = scanned.fedora_release or current.fedora_release
    return f"""\
# porta-winux package designation DRAFT — edit me, then run:  porta-winux pkg adopt
#
# DELETE any line you do NOT want synced between your machines. Nothing is
# ever uninstalled because of this file; it only drives what gets INSTALLED
# on machines that are missing something. Entries marked "not installed on
# this host" came from another machine's designation — keep them unless you
# truly no longer want them anywhere.

[meta]
fedora_release = "{rel}"
generated = "{datetime.now(timezone.utc).isoformat(timespec="seconds")}"

[dnf]
packages = [
{_toml_str_list(pkgs, pkg_notes)}
{base_block}]
# Non-default repos enabled on your machines. `pkg apply` does NOT enable
# repos automatically (repo setup differs per repo); it tells you which are
# missing and how to add them.
repos = [
{_toml_str_list(repos, repo_notes)}
]

[flatpak]
apps = [
{chr(10).join(app_lines)}
]

[flatpak.remotes]
{remote_lines}
"""


def render_configs_draft(etc_paths: list[str], hints: list[tuple[str, str]]) -> str:
    etc_lines = []
    for p in etc_paths:
        if is_sensitive(p):
            etc_lines.append(f'  # "{p}",  # SENSITIVE: likely contains secrets — uncomment only knowingly')
        else:
            etc_lines.append(f'  "{p}",')
    hint_lines = [f'  # "{p}",  # {why}' for p, why in hints]
    return f"""\
# porta-winux config designation DRAFT — edit me, then run:  porta-winux pkg adopt
#
# etc_paths: /etc config files whose content differs from the package default
# on this machine (found via rpm verification). SENSITIVE entries are
# commented out — remember your restic password sits on the drive.
#
# user_paths: suggested per-app config locations (all commented out; uncomment
# what you want). Different programs store settings in wildly different
# formats — porta-winux treats them all as plain files. See PACKAGES.md for
# the few formats needing care (dconf, sqlite-backed apps).
#
# Everything adopted here becomes the 'system-configs' profile:
#   sudo porta-winux snapshot -p system-configs

[configs]
etc_paths = [
{chr(10).join(etc_lines) if etc_lines else "  # (no altered /etc configs detected)"}
]
user_paths = [
{chr(10).join(hint_lines) if hint_lines else "  # (add absolute paths here)"}
]
excludes = []
"""


# -- adopt / diff / apply ---------------------------------------------------------


def adopt_plan(layout: DriveLayout) -> list[str]:
    """Human-readable description of what adopting the existing drafts would
    change. Empty list = no drafts present."""
    lines: list[str] = []
    pkg_draft = layout.root / PACKAGES_DRAFT
    if pkg_draft.exists():
        cur = PackageState.load(layout.root / PACKAGES_NAME)
        new = PackageState.load(pkg_draft)
        add = sorted(set(new.dnf_packages) - set(cur.dnf_packages))
        drop = sorted(set(cur.dnf_packages) - set(new.dnf_packages))
        a_add = sorted({a["id"] for a in new.flatpak_apps} - {a["id"] for a in cur.flatpak_apps})
        a_drop = sorted({a["id"] for a in cur.flatpak_apps} - {a["id"] for a in new.flatpak_apps})
        lines.append(f"packages.toml: +{len(add)} dnf, -{len(drop)} dnf (list only — never uninstalls), "
                     f"+{len(a_add)} flatpak, -{len(a_drop)} flatpak")
        for p in add:
            lines.append(f"    + {p}")
        for p in drop:
            lines.append(f"    - {p}   (leaves installed copies alone everywhere)")
        for p in a_add:
            lines.append(f"    + flatpak {p}")
        for p in a_drop:
            lines.append(f"    - flatpak {p}   (leaves installed copies alone)")
    cfg_draft = layout.root / CONFIGS_DRAFT
    if cfg_draft.exists():
        with open(cfg_draft, "rb") as f:
            data = tomllib.load(f).get("configs", {})
        n = len(data.get("etc_paths", [])) + len(data.get("user_paths", []))
        lines.append(f"configs.toml: {n} designated path(s) -> profile 'system-configs'")
    return lines


def adopt_execute(layout: DriveLayout) -> list[str]:
    done = []
    pkg_draft = layout.root / PACKAGES_DRAFT
    if pkg_draft.exists():
        PackageState.load(pkg_draft)  # validates TOML before promoting
        shutil.move(str(pkg_draft), str(layout.root / PACKAGES_NAME))
        done.append(PACKAGES_NAME)
    cfg_draft = layout.root / CONFIGS_DRAFT
    if cfg_draft.exists():
        with open(cfg_draft, "rb") as f:
            tomllib.load(f)
        shutil.move(str(cfg_draft), str(layout.root / CONFIGS_NAME))
        done.append(CONFIGS_NAME)
    if not done:
        raise PortaWinuxError(
            f"no drafts found on the drive; run 'pkg scan' / 'pkg scan-configs' first"
        )
    return done


@dataclass
class ApplyPlan:
    dnf_to_install: list[str] = field(default_factory=list)
    repos_missing: list[str] = field(default_factory=list)
    remotes_to_add: dict[str, str] = field(default_factory=dict)
    flatpak_to_install: list[dict] = field(default_factory=list)
    extra_local_dnf: list[str] = field(default_factory=list)      # report only
    extra_local_flatpak: list[str] = field(default_factory=list)  # report only
    release_mismatch: str = ""

    @property
    def has_work(self) -> bool:
        return bool(self.dnf_to_install or self.remotes_to_add or self.flatpak_to_install)


def build_apply_plan(layout: DriveLayout) -> ApplyPlan:
    desired = PackageState.load(layout.root / PACKAGES_NAME)
    if not (desired.dnf_packages or desired.flatpak_apps):
        raise PortaWinuxError("no packages.toml on the drive (or it is empty); run 'pkg scan' then 'pkg adopt'")
    plan = ApplyPlan()
    here = fedora_release()
    if desired.fedora_release and here and desired.fedora_release != here:
        plan.release_mismatch = (
            f"list was generated on Fedora {desired.fedora_release}, this is {here}; "
            "some package names may differ"
        )
    rpm_names = installed_rpm_names()
    plan.dnf_to_install = [p for p in desired.dnf_packages if p not in rpm_names]
    # user-installed scan of THIS host, for the report-only extras
    try:
        local_user = set(scan_dnf_packages()[0])
        plan.extra_local_dnf = sorted(local_user - set(desired.dnf_packages))
    except ScanUnavailable:
        pass
    plan.repos_missing = [r for r in desired.dnf_repos if r not in enabled_repo_ids()]
    fp_here = installed_flatpak_ids()
    plan.flatpak_to_install = [a for a in desired.flatpak_apps if a["id"] not in fp_here]
    plan.extra_local_flatpak = sorted(fp_here - {a["id"] for a in desired.flatpak_apps})
    try:
        remotes_here = scan_flatpak_remotes()
    except ScanUnavailable:
        remotes_here = {}
    plan.remotes_to_add = {
        n: u for n, u in desired.flatpak_remotes.items() if n not in remotes_here
    }
    return plan


def print_plan(plan: ApplyPlan) -> None:
    if plan.release_mismatch:
        print(f"note: {plan.release_mismatch}")
    if plan.repos_missing:
        print(f"repos missing here ({len(plan.repos_missing)}) — NOT auto-enabled; set up first:")
        for r in plan.repos_missing:
            hint = ""
            if r.startswith("rpmfusion"):
                hint = "  (https://rpmfusion.org/Configuration)"
            elif r.startswith("copr:"):
                hint = f"  (sudo dnf copr enable {r.split(':', 2)[-1]})"
            print(f"    {r}{hint}")
    print(f"dnf to install: {len(plan.dnf_to_install)}")
    for p in plan.dnf_to_install:
        print(f"    {p}")
    print(f"flatpak remotes to add: {len(plan.remotes_to_add)}")
    for n, u in plan.remotes_to_add.items():
        print(f"    {n} = {u}")
    print(f"flatpak apps to install: {len(plan.flatpak_to_install)}")
    for a in plan.flatpak_to_install:
        print(f"    {a['id']}" + (f"  (from {a['origin']})" if a.get("origin") else ""))
    if plan.extra_local_dnf or plan.extra_local_flatpak:
        print("installed here but not in the list (REPORT ONLY — never removed):")
        for p in plan.extra_local_dnf:
            print(f"    {p}")
        for p in plan.extra_local_flatpak:
            print(f"    flatpak {p}")


def apply_dnf(packages: list[str], dry_run: bool) -> list[str]:
    """Install packages; returns the ones that failed. Tries one transaction
    (skipping unavailable names), then falls back to per-package installs."""
    if not packages:
        return []
    base = _sudo_prefix() + ["dnf", "install", "-y"]
    if _execute(base + ["--skip-unavailable"] + packages, dry_run) == 0:
        return []
    if _execute(base + packages, dry_run) == 0:
        return []
    failed = []
    for p in packages:
        if _execute(base + [p], dry_run) != 0:
            failed.append(p)
    return failed


def apply_flatpak(remotes: dict[str, str], apps: list[dict], dry_run: bool) -> list[str]:
    failed = []
    for name, url in remotes.items():
        if _execute(["flatpak", "remote-add", "--if-not-exists", name, url], dry_run) != 0:
            failed.append(f"remote:{name}")
    for a in apps:
        cmd = ["flatpak", "install", "-y", "--noninteractive"]
        if a.get("origin"):
            cmd.append(a["origin"])
        cmd.append(a["id"])
        if _execute(cmd, dry_run) != 0:
            failed.append(a["id"])
    return failed
