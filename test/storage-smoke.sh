#!/usr/bin/env bash
# Storage-integrity smoke test. Three acts:
#   1. YANK: a write op that never completes must make the NEXT write refuse
#      until verify passes.
#   2. CORRUPTION: flip one byte inside a real restic pack; verify must name
#      the damaged file (and plain restic check must be shown up by it).
#   3. FORMAT (root + loop devices only, skipped elsewhere): partition a fake
#      2 GiB drive with format-drive and confirm labels/types/filesystems.
# Needs: python3, restic. Act 3 additionally: root, losetup, sgdisk, mkfs.*.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
export PATH="$PATH:/usr/sbin:/sbin"
TMP="$(mktemp -d /tmp/porta-winux-storage.XXXXXX)"
LOOPDEV=""
cleanup() {
  [ -n "$LOOPDEV" ] && losetup -d "$LOOPDEV" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
DRIVE="$TMP/drive"; SYS="$TMP/sys"
export PYTHONPATH="$REPO_ROOT" PORTA_WINUX_STATE_DIR="$TMP/state"
st() { python3 -m porta_winux.cli --drive "$DRIVE" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$SYS/home/u"; echo data1 > "$SYS/home/u/f.txt"
python3 -m porta_winux.cli --yes init-drive "$DRIVE" --password stest >/dev/null
grep -q "expected_fstype" "$DRIVE/porta-winux.toml" || fail "init did not pin fstype"

echo "== act 1: simulated yank mid-operation =="
st --root "$SYS" snapshot >/dev/null
rm -f "$DRIVE/.pw-state/clean"     # what an unplugged-mid-write drive looks like
echo data2 > "$SYS/home/u/f.txt"
if st --root "$SYS" snapshot >/dev/null 2>&1; then
  fail "write proceeded after an unclean session"
fi
st --root "$SYS" snapshot --force >/dev/null || fail "--force override broken"
rm -f "$DRIVE/.pw-state/clean"
st verify >/dev/null || fail "verify failed on healthy repo"
test -f "$DRIVE/.pw-state/clean" || fail "passing verify did not clear the dirty flag"
st --root "$SYS" snapshot >/dev/null || fail "write refused after clean verify"

echo "== act 2: at-rest corruption is detected and named =="
PACK=$(find "$DRIVE/repo/data" -type f | head -1)
python3 - "$PACK" <<'PYEOF'
import sys
p = sys.argv[1]
b = bytearray(open(p, "rb").read())
b[len(b)//2] ^= 0xFF
open(p, "wb").write(b)
PYEOF
if st verify >/dev/null 2>"$TMP/verify.err"; then
  fail "verify passed on a corrupted pack"
fi
grep -q "CORRUPTION" "$TMP/verify.err" || fail "corruption not reported"
grep -q "$(basename "$PACK")" "$TMP/verify.err" || fail "damaged pack not named"
grep -q "STORAGE.md" "$TMP/verify.err" || fail "recovery guidance missing"
# corrupted state must also block writes (verify failed -> marker still absent)
if st --root "$SYS" snapshot >/dev/null 2>&1; then
  fail "write proceeded on a repo that just failed verification"
fi

echo "== act 3: format-drive on a loop device =="
if [ "$(id -u)" -ne 0 ] || ! command -v losetup >/dev/null \
   || ! command -v sgdisk >/dev/null || ! command -v mkfs.exfat >/dev/null \
   || ! command -v mkfs.btrfs >/dev/null || ! command -v mkfs.ntfs >/dev/null; then
  echo "   (skipped: needs root + losetup + sgdisk + mkfs.{exfat,btrfs,ntfs})"
else
  truncate -s 2G "$TMP/fakedisk.img"
  LOOPDEV=$(losetup -f --show -P "$TMP/fakedisk.img")
  echo "   fake drive: $LOOPDEV"
  # dry-run prints, executes nothing
  python3 -m porta_winux.cli format-drive "$LOOPDEV" \
      --shared 256M --linux 1G -n | grep -q "DRY-RUN" || fail "dry-run silent"
  blkid "${LOOPDEV}p1" 2>/dev/null && fail "dry-run formatted something"
  # real run, feeding the typed confirmation through a pty
  printf '%s\n' "$LOOPDEV" | script -qec \
    "python3 -m porta_winux.cli format-drive $LOOPDEV --shared 256M --linux 1G" \
    /dev/null >/dev/null
  { partprobe "$LOOPDEV" 2>/dev/null || partx -u "$LOOPDEV" || true; }; sleep 1
  # blkid -p probes devices directly (lsblk needs a udev daemon to see labels)
  fscheck() { blkid -p -o value -s TYPE -s LABEL "$1" | tr '\n' ' '; }
  fscheck "${LOOPDEV}p1" | grep -qi "exfat.*PW_SHARED\|PW_SHARED.*exfat" || fail "PW_SHARED wrong: $(fscheck ${LOOPDEV}p1)"
  fscheck "${LOOPDEV}p2" | grep -qi "btrfs.*PW_LINUX\|PW_LINUX.*btrfs" || fail "PW_LINUX wrong: $(fscheck ${LOOPDEV}p2)"
  fscheck "${LOOPDEV}p3" | grep -qi "ntfs.*PW_WIN\|PW_WIN.*ntfs"       || fail "PW_WIN wrong: $(fscheck ${LOOPDEV}p3)"
  TYPE2=$(sgdisk -i 2 "$LOOPDEV" | grep "Partition GUID code" )
  echo "$TYPE2" | grep -qi "0FC63DAF-8483-4772-8E79-3D69D8477DE4" \
    || fail "PW_LINUX is not GPT type 8300 (Windows would offer to format it!)"
  echo "   layout verified: labels, filesystems, and the 8300 type GUID"
  # passing a PARTITION must be caught with a pointer to the whole disk
  if OUT=$(python3 -m porta_winux.cli format-drive "${LOOPDEV}p1" -n 2>&1); then
    fail "format-drive accepted a partition"
  fi
  echo "$OUT" | grep -q "PARTITION" || fail "partition mistake not explained"
  echo "$OUT" | grep -q "$LOOPDEV" || fail "whole-disk suggestion missing"
  # a mounted partition must be named with an unmount command
  MNT=$(mktemp -d)
  if mount "${LOOPDEV}p2" "$MNT" 2>/dev/null; then
    if OUT=$(python3 -m porta_winux.cli format-drive "$LOOPDEV" -n 2>&1); then
      umount "$MNT"; fail "format-drive accepted a disk with mounted partitions"
    fi
    umount "$MNT"
    echo "$OUT" | grep -q "udisksctl unmount" || fail "unmount guidance missing"
    echo "   wrong-device mistakes are caught with actionable messages"
  else
    echo "   (mounted-partition check skipped: this kernel cannot mount loop filesystems)"
  fi
  rmdir "$MNT"
fi

echo
echo "ALL STORAGE TESTS PASSED  (sandbox: $TMP)"
