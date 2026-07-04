# custom_borg_sync (Linux/Fedora scope)

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

```bash
sudo dnf install borgbackup borgmatic ansible rsync python3-pyyaml python3-jinja2
ansible-galaxy collection install community.general   # for the flatpak module

git clone <this repo> ~/custom_borg_sync
sudo mkdir -p /mnt/backupdrive
sudo cp ~/custom_borg_sync/systemd/borg-drive-handler@.service /etc/systemd/system/
sudo systemctl daemon-reload

# find your drive's filesystem UUID
sudo blkid /dev/sdX1
# edit udev/99-backup-drive.rules with that UUID, then:
sudo cp ~/custom_borg_sync/udev/99-backup-drive.rules /etc/udev/rules.d/
sudo udevadm control --reload
```

Edit `manifest/manifest.yaml` to add this host under `hosts:` with its
actual paths.

**First-ever run per host** needs to `borg init` the repo before borgmatic
can use it:
```bash
mkdir -p /mnt/backupdrive/Backups
borg init --encryption=repokey-blake2 /mnt/backupdrive/Backups/$(hostname -s).borgrepo
```

## Trigger reliability note

The udev rule fires on device *arrival*, before the filesystem is
necessarily mounted. If your desktop environment auto-mounts removable
media (GNOME/KDE do, by default) this is fine in practice — the mount
finishes before `drive_handler.sh`'s `mountpoint -q` check runs. If you're
on a minimal/tiling setup without automount, mount explicitly first (or add
a `systemd.mount` unit for the drive) — the handler will simply fail fast
with a clear error ("is not mounted") rather than doing anything
half-attached.

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
