"""Drive layout discovery and manifest (porta-winux.toml) parsing."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import DriveNotFound, PortaWinuxError

MANIFEST_NAME = "porta-winux.toml"


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

    def load_manifest(self) -> Manifest:
        if not self.manifest_path.exists():
            raise DriveNotFound(f"no {MANIFEST_NAME} at {self.root}")
        return Manifest.load(self.manifest_path)


def find_drive(explicit: str | None) -> DriveLayout:
    """Locate the drive root.

    Order: --drive flag, $PORTA_WINUX_DRIVE, then a scan of common removable
    mount points for a porta-winux.toml.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    elif os.environ.get("PORTA_WINUX_DRIVE"):
        candidates.append(Path(os.environ["PORTA_WINUX_DRIVE"]))
    else:
        user = os.environ.get("USER", "")
        for base in (Path(f"/run/media/{user}"), Path("/media"), Path("/mnt")):
            if base.is_dir():
                try:
                    candidates.extend(sorted(base.iterdir()))
                except PermissionError:
                    pass

    for c in candidates:
        if (c / MANIFEST_NAME).exists():
            return DriveLayout(root=c.resolve())

    hint = explicit or os.environ.get("PORTA_WINUX_DRIVE") or "auto-detect"
    raise DriveNotFound(
        f"could not find a porta-winux drive ({hint}); "
        f"pass --drive /path/to/mounted/drive or set PORTA_WINUX_DRIVE"
    )
