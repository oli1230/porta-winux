"""Drive layout discovery and manifest (porta-winux.toml) parsing."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import DriveNotFound, PortaWinuxError

MANIFEST_NAME = "porta-winux.toml"
SIGNATURE_NAME = ".porta-winux"   # machine-readable "this is a porta-winux drive"
STATE_DIRNAME = ".pw-state"       # clean marker + operation journal


@dataclass
class Profile:
    name: str
    paths: list[str]
    excludes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.paths:
            raise PortaWinuxError(f"profile '{self.name}' has no paths")
        for p in self.paths:
            if not p.startswith("/"):
                raise PortaWinuxError(f"profile '{self.name}': path '{p}' must be absolute")


@dataclass
class Manifest:
    profiles: dict[str, Profile]
    default_profile: str

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        raw_profiles = data.get("profiles", {})
        if not raw_profiles:
            raise PortaWinuxError(f"{path}: no [profiles.*] defined")
        profiles: dict[str, Profile] = {}
        for name, body in raw_profiles.items():
            prof = Profile(
                name=name,
                paths=list(body.get("paths", [])),
                excludes=list(body.get("excludes", [])),
            )
            prof.validate()
            profiles[name] = prof
        default = data.get("porta-winux", {}).get("default_profile", next(iter(profiles)))
        if default not in profiles:
            raise PortaWinuxError(f"default_profile '{default}' is not a defined profile")
        return cls(profiles=profiles, default_profile=default)

    def profile(self, name: str | None) -> Profile:
        name = name or self.default_profile
        if name not in self.profiles:
            raise PortaWinuxError(
                f"unknown profile '{name}' (have: {', '.join(sorted(self.profiles))})"
            )
        return self.profiles[name]


@dataclass
class DriveLayout:
    """Resolved paths on a porta-winux drive."""

    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def password_file(self) -> Path:
        return self.root / ".restic-pass"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def workspace_root(self) -> Path:
        return self.workspace / "root"

    @property
    def hooks_dir(self) -> Path:
        return self.root / "hooks.d"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def configs_path(self) -> Path:
        return self.root / "configs.toml"

    @property
    def signature_path(self) -> Path:
        return self.root / SIGNATURE_NAME

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.jsonl"

    def is_drive(self) -> bool:
        """A drive is recognized by its signature file, or (drives made by
        versions before 0.4) by the manifest alone."""
        return self.signature_path.exists() or self.manifest_path.exists()

    def read_signature(self) -> dict | None:
        """Contents of .porta-winux, or None when absent/unreadable."""
        try:
            return json.loads(self.signature_path.read_text())
        except (OSError, ValueError):
            return None

    def write_signature(self, version: str) -> dict:
        """Create the signature if missing (idempotent: an existing drive id
        is never changed, because host state and logs refer to it)."""
        sig = self.read_signature()
        if sig and sig.get("drive_id"):
            return sig
        import socket
        import uuid
        from datetime import datetime, timezone
        sig = {
            "format": 1,
            "drive_id": str(uuid.uuid4()),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "created_by": socket.gethostname(),
            "created_with": f"porta-winux {version}",
        }
        self.signature_path.write_text(json.dumps(sig, indent=2) + "\n")
        return sig

    def load_manifest(self) -> Manifest:
        if not self.manifest_path.exists():
            raise DriveNotFound(f"no {MANIFEST_NAME} at {self.root}")
        m = Manifest.load(self.manifest_path)
        # Designated configs (from `pkg scan-configs` + `pkg adopt`) become a
        # normal profile, so snapshot/checkout/commit/sync all just work on them.
        if self.configs_path.exists():
            with open(self.configs_path, "rb") as f:
                data = tomllib.load(f).get("configs", {})
            paths = list(data.get("etc_paths", [])) + list(data.get("user_paths", []))
            if paths:
                prof = Profile(
                    name="system-configs",
                    paths=paths,
                    excludes=list(data.get("excludes", [])),
                )
                prof.validate()
                m.profiles["system-configs"] = prof
        return m


def mount_candidates() -> list[Path]:
    """Directories where a plugged-in drive typically appears."""
    out: list[Path] = []
    user = os.environ.get("USER", "")
    for base in (Path(f"/run/media/{user}"), Path("/media"), Path("/mnt")):
        if base.is_dir():
            try:
                out.extend(p for p in sorted(base.iterdir()) if p.is_dir())
            except PermissionError:
                pass
    return out


def drive_candidates(explicit: str | None) -> list[Path]:
    """Where to look, in order: --drive, $PORTA_WINUX_DRIVE, then the usual
    removable mount points."""
    if explicit:
        return [Path(explicit)]
    if os.environ.get("PORTA_WINUX_DRIVE"):
        return [Path(os.environ["PORTA_WINUX_DRIVE"])]
    return mount_candidates()


def scan_drives(explicit: str | None = None) -> list[DriveLayout]:
    """Every porta-winux drive visible right now (read-only, never raises)."""
    return [DriveLayout(root=c.resolve()) for c in drive_candidates(explicit)
            if DriveLayout(root=c).is_drive()]


def find_drive(explicit: str | None) -> DriveLayout:
    """Locate the drive root: the first candidate carrying a .porta-winux
    signature or a porta-winux.toml manifest."""
    found = scan_drives(explicit)
    if found:
        return found[0]
    hint = explicit or os.environ.get("PORTA_WINUX_DRIVE") or "auto-detect"
    raise DriveNotFound(
        f"could not find a porta-winux drive ({hint}); "
        f"pass --drive /path/to/mounted/drive or set PORTA_WINUX_DRIVE. "
        f"To see what IS plugged in: porta-winux init detect"
    )
