"""hooks.d runner: the primary extension point for future modules.

A hook is a runnable program in <drive>/hooks.d/<event>/ — a script
starting with a #! shebang line, or a compiled binary. Hooks run in
lexical order and receive context via PORTA_WINUX_* environment variables:

    PORTA_WINUX_DRIVE      drive root
    PORTA_WINUX_EVENT      event name
    PORTA_WINUX_ROOT       target system root ("/" normally, sandbox in tests)
    PORTA_WINUX_SNAPSHOT   relevant snapshot id, when applicable
    PORTA_WINUX_PROFILE    profile name, when applicable
    PORTA_WINUX_PATHS      newline-separated affected paths, when applicable

Events in v1: pre-snapshot, post-snapshot, pre-checkout, post-sync.
A non-zero exit from a pre-* hook aborts the operation; post-* hook
failures are reported but non-fatal.

Why not just "any executable file"? External drives are usually formatted
FAT/exFAT/NTFS, which don't store Unix permission bits — Linux mounts them
with EVERY file marked executable. So the +x bit proves nothing there, and
placeholders like .keep or README.md would get "executed" and crash. We
therefore only run files the kernel can actually execute (shebang or ELF
magic) and always skip dotfiles and documentation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import PortaWinuxError
from .manifest import DriveLayout

PRE_EVENTS = {"pre-snapshot", "pre-checkout"}
_DOC_SUFFIXES = {".md", ".txt", ".rst"}


def _is_runnable(path: Path) -> bool:
    """True only for files the kernel can execute: #! scripts or ELF binaries."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head[:2] == b"#!" or head == b"\x7fELF"


def run_hooks(layout: DriveLayout, event: str, context: dict[str, str], root: str = "/") -> None:
    hook_dir = layout.hooks_dir / event
    if not hook_dir.is_dir():
        return
    env = {
        **os.environ,
        "PORTA_WINUX_DRIVE": str(layout.root),
        "PORTA_WINUX_EVENT": event,
        "PORTA_WINUX_ROOT": root,
        **{f"PORTA_WINUX_{k.upper()}": v for k, v in context.items()},
    }
    for hook in sorted(hook_dir.iterdir()):
        if not hook.is_file():
            continue
        name = hook.name
        # Placeholders and documentation are never hooks, whatever their mode bits.
        if name.startswith(".") or name.lower().startswith("readme") or hook.suffix in _DOC_SUFFIXES:
            continue
        if not os.access(hook, os.X_OK):
            continue
        if not _is_runnable(hook):
            print(
                f"warning: skipping hook {event}/{name}: not a runnable program "
                "(scripts must start with a #! line, e.g. #!/bin/bash)",
                file=sys.stderr,
            )
            continue
        try:
            rc = subprocess.run([str(hook)], env=env).returncode
        except OSError as e:
            rc = None
            print(f"warning: could not execute hook {event}/{name}: {e}", file=sys.stderr)
        if rc == 0:
            continue
        msg = f"hook {event}/{name} " + (f"exited {rc}" if rc is not None else "failed to start")
        if event in PRE_EVENTS:
            raise PortaWinuxError(msg + " (aborting)")
        print(f"warning: {msg}", file=sys.stderr)
