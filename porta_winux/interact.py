"""Human-in-the-loop confirmation for every operation that writes or
removes data.

Policy:
  - Any command that writes to a system, a drive, or removes snapshots
    shows a concrete plan (what, where) and asks before proceeding.
  - `--yes` skips prompts — for scripts, kickstart %post, and tests.
  - With no terminal attached (cron, CI) and no `--yes`, the command
    refuses rather than guessing. Silence is never consent.

Purely additive operations (taking a snapshot, staging a checkout into
the drive's workspace, committing) don't prompt: they can't destroy
anything and are always reversible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import PortaWinuxError


def _has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str, assume_yes: bool = False) -> None:
    """Show `question`, require an explicit yes. Raises on 'no' or no-TTY."""
    if assume_yes:
        return
    if not _has_tty():
        raise PortaWinuxError(
            "this operation writes data and needs confirmation, but no "
            "terminal is attached. Re-run with --yes to proceed non-interactively."
        )
    ans = input(f"{question} [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        raise PortaWinuxError("aborted by user (nothing was written)")


def choose(title: str, options: list[str], allow_other: str | None = None) -> str:
    """Numbered menu; returns the chosen option string.

    If `allow_other` is set, an extra entry lets the user type a value
    (used by init-drive to enter a path manually).
    """
    if not _has_tty():
        raise PortaWinuxError(
            "an interactive choice is needed but no terminal is attached; "
            "pass the value explicitly on the command line instead."
        )
    print(title)
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")
    extra = len(options) + 1
    if allow_other:
        print(f"  {extra}) {allow_other}")
    while True:
        raw = input(f"choose [1-{extra if allow_other else len(options)}, q aborts]: ").strip()
        if raw.lower() == "q":
            raise PortaWinuxError("aborted by user (nothing was written)")
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(options):
                return options[n - 1]
            if allow_other and n == extra:
                val = input("enter value: ").strip()
                if val:
                    return val
        print("  (invalid choice)")


def removable_mount_candidates() -> list[str]:
    """Directories where a plugged-in drive typically appears."""
    out: list[str] = []
    user = os.environ.get("USER", "")
    for base in (Path(f"/run/media/{user}"), Path("/media"), Path("/mnt")):
        if base.is_dir():
            try:
                out.extend(str(p) for p in sorted(base.iterdir()) if p.is_dir())
            except PermissionError:
                pass
    return out
