#!/usr/bin/env bash
# Confirms the backup drive is mounted where Dolphin/GNOME Files auto-mount
# it (/run/media/$USER/<LABEL>) — and that it's actually the right physical
# drive, not just something with a matching label. Waits up to --timeout
# seconds since udisks2's mount can lag slightly behind udev's device-add
# event that triggers this whole pipeline.
#
# On success: prints the mount path on stdout, exits 0.
# On failure:  prints nothing on stdout (safe to capture in a variable),
#              exits 1 (not mounted in time) or 2 (mounted, wrong drive).
#
# Usage:
#   check_drive_mounted.sh --label OliDrive [--uuid <expected-fs-uuid>] [--timeout 30]
#
# Get the UUID once with: findmnt -no UUID /run/media/$USER/OliDrive
# (or: lsblk -f). Passing --uuid is optional but recommended — it's the
# only thing that actually distinguishes "your" drive from a same-named one.
set -euo pipefail

LABEL="OliDrive"
EXPECTED_UUID=""
TIMEOUT=30
INTERVAL=2

while [[ $# -gt 0 ]]; do
    case "$1" in
        --label)   LABEL="$2"; shift 2 ;;
        --uuid)    EXPECTED_UUID="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

RUN_AS_USER="${USER:-$(whoami)}"
MOUNT_PATH="/run/media/${RUN_AS_USER}/${LABEL}"

elapsed=0
while ! mountpoint -q "$MOUNT_PATH" 2>/dev/null; do
    if (( elapsed >= TIMEOUT )); then
        echo "ERROR: $MOUNT_PATH is not mounted after ${TIMEOUT}s" >&2
        exit 1
    fi
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
done

if [[ -n "$EXPECTED_UUID" ]]; then
    actual_uuid="$(findmnt -no UUID "$MOUNT_PATH" 2>/dev/null || true)"
    if [[ "$actual_uuid" != "$EXPECTED_UUID" ]]; then
        echo "ERROR: $MOUNT_PATH is mounted but its filesystem UUID ($actual_uuid)" >&2
        echo "       does not match the expected UUID ($EXPECTED_UUID)." >&2
        echo "       Refusing to use it — this may be a different drive with the same label." >&2
        exit 2
    fi
fi

echo "$MOUNT_PATH"