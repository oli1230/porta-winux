"""porta-winux: git-like checkout/commit/sync semantics over a restic snapshot store.

Layout of a porta-winux drive (created by init-drive):

    <drive-root>/
    ├── .porta-winux       signature: drive id, creation time/host/version
    ├── porta-winux.toml   manifest: profiles, options (the file you edit)
    ├── .pw-state/         clean marker (drivehealth) + journal.jsonl (ops)
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
    safety, pre:<cmd>   automatic pre-sync/pre-restore state capture
    op:<id>, via:<cmd>, forced   which operation produced it (see ops.py)

Host-side state lives in ~/.local/state/porta-winux/<repo-id>.json (or
$PORTA_WINUX_STATE_DIR) and records the last applied commit and the base
system snapshot used for conflict detection.
"""

__version__ = "0.4.0"


def hostname() -> str:
    """This machine's name as recorded in snapshots. $PORTA_WINUX_HOSTNAME
    overrides it (tests simulate a second machine that way)."""
    import os
    import socket
    return os.environ.get("PORTA_WINUX_HOSTNAME") or socket.gethostname()


class PortaWinuxError(Exception):
    """Base class for user-facing errors. CLI prints these without a traceback."""


class DriveNotFound(PortaWinuxError):
    pass


class StoreError(PortaWinuxError):
    pass


class ConflictError(PortaWinuxError):
    pass
