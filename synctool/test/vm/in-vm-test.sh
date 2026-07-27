#!/usr/bin/env bash
# Runs inside the test VM (as root, via vm.sh test).
# Tier 1: the same smoke test as on the host, proving the flow on Fedora 43.
# Tier 2: VM-only checks — drive auto-detection on a real removable-style
# mount, and hooks firing with SELinux present.
set -euxo pipefail
SRC=~test/synctool
DRIVE=/run/media/test/SYNCDRIVE

# Tier 1
bash "$SRC/test/smoke.sh"

# Tier 2: auto-detection (no --drive flag, no env) against the mounted disk
rm -rf "$DRIVE/repo" "$DRIVE/workspace"
export PYTHONPATH="$SRC/src"
export SYNCTOOL_STATE_DIR=/tmp/synctool-vm-state
export USER=test
python3 -m synctool.cli init-drive "$DRIVE" --password vmtest >/dev/null
mkdir -p /tmp/vmsys/home/test
echo data > /tmp/vmsys/home/test/f.txt
python3 -m synctool.cli --root /tmp/vmsys snapshot -p home   # note: no --drive
python3 -m synctool.cli list | grep -q system

# Tier 2: hook execution
mkdir -p "$DRIVE/hooks.d/post-snapshot"
cat > "$DRIVE/hooks.d/post-snapshot/99-touch" <<'HOOK'
#!/bin/bash
touch /tmp/hook-ran
HOOK
chmod +x "$DRIVE/hooks.d/post-snapshot/99-touch"
python3 -m synctool.cli --root /tmp/vmsys snapshot -p home >/dev/null
test -f /tmp/hook-ran

echo "ALL VM TESTS PASSED"
