"""hooks.d runner: the primary extension point for future modules.

A hook is any executable file in <drive>/hooks.d/<event>/. Hooks run in
lexical order and receive context via SYNCTOOL_* environment variables:

    SYNCTOOL_DRIVE      drive root
    SYNCTOOL_EVENT      event name
    SYNCTOOL_ROOT       target system root ("/" normally, sandbox in tests)
    SYNCTOOL_SNAPSHOT   relevant snapshot id, when applicable
    SYNCTOOL_PROFILE    profile name, when applicable
    SYNCTOOL_PATHS      newline-separated affected paths, when applicable

Events in v1: pre-snapshot, post-snapshot, pre-checkout, post-sync.
A non-zero exit from a pre-* hook aborts the operation; post-* hook
failures are reported but non-fatal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import SynctoolError
from .manifest import DriveLayout

PRE_EVENTS = {"pre-snapshot", "pre-checkout"}


def run_hooks(layout: DriveLayout, event: str, context: dict[str, str], root: str = "/") -> None:
    hook_dir = layout.hooks_dir / event
    if not hook_dir.is_dir():
        return
    env = {
        **os.environ,
        "SYNCTOOL_DRIVE": str(layout.root),
        "SYNCTOOL_EVENT": event,
        "SYNCTOOL_ROOT": root,
        **{f"SYNCTOOL_{k.upper()}": v for k, v in context.items()},
    }
    for hook in sorted(hook_dir.iterdir()):
        if not (hook.is_file() and os.access(hook, os.X_OK)):
            continue
        proc = subprocess.run([str(hook)], env=env)
        if proc.returncode != 0:
            msg = f"hook {event}/{hook.name} exited {proc.returncode}"
            if event in PRE_EVENTS:
                raise SynctoolError(msg + " (aborting)")
            print(f"warning: {msg}", file=sys.stderr)
