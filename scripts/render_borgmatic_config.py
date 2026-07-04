#!/usr/bin/env python3
"""
Render a per-host borgmatic config from manifest/manifest.yaml.

This keeps manifest.yaml as the single source of truth: nobody should
hand-edit borgmatic/generated/<host>.yaml directly, because this script
overwrites it on every run (drive_handler.sh calls it before every backup).

Usage:
    render_borgmatic_config.py --host desktop-fedora \
        --manifest manifest/manifest.yaml \
        --drive-mount /mnt/backupdrive \
        --output borgmatic/generated/desktop-fedora.yaml
"""
import argparse
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--drive-mount", required=True, type=Path,
                         help="Where the external drive is currently mounted")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--template-dir", default=None, type=Path)
    args = parser.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text())

    if args.host not in manifest["hosts"]:
        print(f"ERROR: host '{args.host}' not found in {args.manifest}", file=sys.stderr)
        return 1

    host_cfg = manifest["hosts"][args.host]
    paths = [p["path"] for p in host_cfg["paths"]
             if "both" in p["tags"] or "borg-only" in p["tags"]]

    repo_root = manifest["borg"]["repo_root"].lstrip("/")
    repo_path = str(args.drive_mount / repo_root / f"{args.host}.borgrepo")
    log_path = str(args.drive_mount / "logs" / f"{args.host}.log")

    template_dir = args.template_dir or (Path(__file__).parent.parent / "borgmatic" / "templates")
    env = Environment(loader=FileSystemLoader(str(template_dir)), trim_blocks=True, lstrip_blocks=True)
    template = env.get_template("config.yaml.j2")

    rendered = template.render(
        host=args.host,
        paths=paths,
        excludes=manifest.get("global_excludes", []),
        retention=manifest["borg"]["retention"],
        repo_path=repo_path,
        log_path=log_path,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
