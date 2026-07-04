#!/usr/bin/env python3
"""
Ledger-aware live sync between a machine's local folders and the
/LiveSync area on the physically-moved external drive.

Why a ledger instead of plain rsync:
Because the drive is only ever attached to one machine at a time, a naive
"always push" or "always pull" will eventually silently clobber changes
made on the other machine. The ledger is a small JSON file living ON THE
DRIVE that records, per top-level synced folder: which host last wrote to
it, when, and a monotonically increasing generation number. Each host also
keeps a local cache of the last generation it successfully reconciled.

Decision per folder, each time the drive is attached:
  1. No ledger entry yet                -> first push, safe.
  2. Ledger's last-writer == this host  -> we're continuing our own chain, safe to push.
  3. Ledger's last-writer == other host:
       a. local copy has NOT changed since our last reconcile -> safe to pull.
       b. local copy HAS changed since our last reconcile     -> CONFLICT.
          Do not touch either side; log it and let the human resolve it
          (rerun with --resolve push|pull to force a direction).

This intentionally refuses to guess in the conflict case. A backup/sync
tool that silently picks a side on a conflict is worse than one that stops
and asks.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def newest_mtime(folder: Path) -> float:
    """Newest mtime among all files under folder, recursively. 0 if empty/missing."""
    if not folder.exists():
        return 0.0
    newest = 0.0
    for p in folder.rglob("*"):
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
    return newest


def rsync(src: str, dst: str, delete: bool) -> None:
    cmd = ["rsync", "-a", "--itemize-changes"]
    if delete:
        cmd.append("--delete")
    cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--drive-mount", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--resolve", choices=["push", "pull"], default=None,
                     help="Force a direction for any folder currently in conflict")
    ap.add_argument("--allow-delete", action="store_true",
                     help="Pass --delete to rsync (off by default: safer to leave stragglers than delete wrongly)")
    args = ap.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text())
    host_cfg = manifest["hosts"].get(args.host)
    if host_cfg is None:
        print(f"ERROR: unknown host {args.host}", file=sys.stderr)
        return 1

    livesync_cfg = manifest["livesync"]
    ledger_path = args.drive_mount / livesync_cfg["root"].lstrip("/") / livesync_cfg["ledger_filename"]
    local_cache_path = Path(livesync_cfg["local_cache"]).expanduser()

    ledger = load_json(ledger_path, {})
    local_cache = load_json(local_cache_path, {})

    conflicts = []

    for entry in host_cfg["paths"]:
        if "both" not in entry["tags"] and "livesync-only" not in entry["tags"]:
            continue

        local_path = Path(entry["path"]).expanduser()
        folder_key = local_path.name  # top-level folder name is the ledger key
        drive_path = args.drive_mount / livesync_cfg["root"].lstrip("/") / folder_key

        ledger_entry = ledger.get(folder_key)
        cache_entry = local_cache.get(folder_key, {"generation": -1})

        print(f"[{folder_key}] local={local_path} drive={drive_path}")

        forced = args.resolve
        if ledger_entry is None:
            direction = "push"
        elif ledger_entry["host"] == args.host:
            direction = "push"
        elif cache_entry["generation"] == ledger_entry["generation"]:
            direction = "pull"
        elif forced:
            direction = forced
            print(f"  CONFLICT overridden by --resolve {forced}")
        else:
            print(f"  CONFLICT: drive last written by '{ledger_entry['host']}' "
                  f"(gen {ledger_entry['generation']}) but local has unreconciled changes "
                  f"(cached gen {cache_entry['generation']}). Skipping — rerun with "
                  f"--resolve push|pull to force this folder.")
            conflicts.append(folder_key)
            continue

        drive_path.mkdir(parents=True, exist_ok=True)

        if direction == "push":
            rsync(str(local_path), str(drive_path), delete=args.allow_delete)
            new_gen = (ledger_entry["generation"] + 1) if ledger_entry else 0
            ledger[folder_key] = {"host": args.host, "timestamp": time.time(), "generation": new_gen}
            local_cache[folder_key] = {"generation": new_gen}
            print(f"  pushed -> ledger gen {new_gen}")
        else:
            rsync(str(drive_path), str(local_path), delete=args.allow_delete)
            local_cache[folder_key] = {"generation": ledger_entry["generation"]}
            print(f"  pulled <- ledger gen {ledger_entry['generation']}")

    save_json(ledger_path, ledger)
    save_json(local_cache_path, local_cache)

    if conflicts:
        print(f"\n{len(conflicts)} folder(s) skipped due to conflicts: {', '.join(conflicts)}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
