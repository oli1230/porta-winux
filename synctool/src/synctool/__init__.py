"""synctool: git-like checkout/commit/sync semantics over a restic snapshot store.

Layout of a synctool drive (see drive-template/):

    <drive-root>/
    ├── synctool.toml      manifest: profiles, options (marks the drive root)
    ├── repo/              restic repository (the only bulk data store)
    ├── .restic-pass       repo password (see README for the security tradeoff)
    ├── workspace/
    │   └── root/          checked-out files, mirrored by absolute path
    ├── hooks.d/
    │   ├── pre-snapshot/  executables run before a system snapshot
    │   ├── post-snapshot/
    │   ├── pre-checkout/
    │   └── post-sync/     e.g. restorecon on Fedora
    └── bin/               optional static restic binary for foreign PCs

Snapshot taxonomy (restic tags):
    system              full snapshot of manifest profile paths from a host
    profile:<name>      which profile produced a system snapshot
    commit              a workspace commit (edits made on a foreign PC)

Host-side state lives in ~/.local/state/synctool/<repo-id>.json (or
$SYNCTOOL_STATE_DIR) and records the last applied commit and the base
system snapshot used for conflict detection.
"""

__version__ = "0.1.0"


class SynctoolError(Exception):
    """Base class for user-facing errors. CLI prints these without a traceback."""


class DriveNotFound(SynctoolError):
    pass


class StoreError(SynctoolError):
    pass


class ConflictError(SynctoolError):
    pass
