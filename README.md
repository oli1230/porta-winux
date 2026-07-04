# porta-winux (Linux/Fedora scope)

Backup + sync system for two Fedora machines (desktop, laptop) sharing one
external drive that gets physically moved between them. Windows-side work
is deferred; the Ansible layer is written to extend to it later.

## Core design principle

**Automatic actions are read-only or additive. Anything that installs,
overwrites, or deletes requires a human to run it on purpose.**

- Automatic on drive-attach (`scripts/drive_handler.sh`): gather software
  facts, ledger-aware live sync, borgmatic backup, periodic integrity check.
- Manual-only: `ansible/playbooks/ensure_packages.yml` (installs software),
  `ledger_sync.py --resolve push|pull` (forces a conflict resolution),
  `--allow-delete` on the sync (off by default).

## Layout

```
manifest/manifest.yaml       <- single source of truth: what gets backed up/synced, per host
manifest/packages/*.yml      <- auto-generated software inventory, per host (commit these)
borgmatic/templates/         <- Jinja2 template borgmatic configs are rendered from
borgmatic/generated/         <- output of render_borgmatic_config.py (gitignore-able, regenerated each run)
scripts/
  render_borgmatic_config.py <- manifest.yaml -> borgmatic/generated/<host>.yaml
  ledger_sync.py             <- conflict-aware rsync between local folders and drive's /LiveSync
  drive_handler.sh           <- orchestrator; this is what systemd actually runs
ansible/
  playbooks/gather_facts.yml     <- read-only, safe to automate
  playbooks/ensure_packages.yml  <- installs stuff, manual-only
systemd/borg-drive-handler@.service
udev/99-backup-drive.rules
```

## One-time setup per machine

Check what's missing, then install only that, with confirmation at each step:
```bash
git clone https://github.com/oli1230/porta-winux.git ~/porta-winux
cd ~/porta-winux
make check          # reports missing dnf packages, installs nothing
make install-deps   # installs only what's missing, asks first
make check-collection
make install-collection

# Edit `manifest/manifest.yaml` to add this host under `hosts:` with its
# actual paths.

make detect-uuid       # drive plugged in; writes UUID into manifest.yaml, asks first
make install-udev-rule # renders the rule with that UUID, installs/updates under /etc, asks first
make install-service   # installs/updates the systemd unit, asks first
make borg-init         # inits the repo on the drive, only if one isn't already there
```

## Trigger reliability note

The udev rule fires on device *arrival*, before udisks2 necessarily finishes
mounting it to `/run/media/$USER/<label>`. That's handled: `drive_handler.sh`
calls `scripts/check_drive_mounted.sh`, which polls (default 30s) for the
mount to appear and, if `drive.expected_uuid` is set, verifies it's actually
the right physical drive before doing anything else — otherwise it fails
fast with a clear error rather than writing to the wrong place.

## Restoring a machine from scratch

1. Fresh Fedora install.
2. `git clone` this repo, install the one-time-setup packages above.
3. `ansible-playbook ansible/playbooks/ensure_packages.yml --extra-vars "target_host=<host>" --ask-become-pass`
   — reinstalls your software from the last committed manifest.
4. `borgmatic -c borgmatic/generated/<host>.yaml restore ...` (or `borg extract`)
   for your actual data, once the drive is attached and the config is
   regenerated.

## What's intentionally not built yet

- Windows side (native Borg fork evaluation, Ansible `win_*` modules) —
  revisit once this Linux path is solid.
- Off-drive redundancy (this whole system has one physical copy; consider
  an occasional `borg` push to a remote/cloud repo for the truly
  irreplaceable subset of `manifest.yaml`).
