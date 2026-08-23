#!/usr/bin/env bash
# Fast smoke test: full snapshot → checkout → commit → sync → conflict →
# revert → fresh-restore cycle in a throwaway sandbox. Needs only restic
# and python3; safe to run on any machine (never touches real /).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

TMP="$(mktemp -d /tmp/porta-winux-smoke.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
DRIVE="$TMP/drive"
SYSA="$TMP/sysA"
SYSB="$TMP/sysB"
export PYTHONPATH="$REPO_ROOT"
export PORTA_WINUX_STATE_DIR="$TMP/state"

st() { python3 -m porta_winux.cli --drive "$DRIVE" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== init drive"
mkdir -p "$SYSA/home/user"
echo "hello v1"  > "$SYSA/home/user/notes.txt"
echo "config v1" > "$SYSA/home/user/app.conf"
python3 -m porta_winux.cli --yes init-drive "$DRIVE" --password smokepass >/dev/null

echo "== simulate exFAT drive: mark ALL drive files executable (regression test)"
find "$DRIVE" -type f -exec chmod +x {} +

echo "== system snapshot (machine A)"
st --root "$SYSA" snapshot -p home >/dev/null
st list

echo "== checkout + edit + commit (foreign PC)"
st checkout "$SYSA/home/user/notes.txt" >/dev/null
echo "hello v2 (edited elsewhere)" > "$DRIVE/workspace/root$SYSA/home/user/notes.txt"
st commit -m "edit notes on the road" >/dev/null

echo "== sync back to machine A"
if st sync --no-snapshot >/dev/null 2>&1; then
  fail "sync wrote data without confirmation (no TTY, no --yes)"
fi
grep -q "hello v1" "$SYSA/home/user/notes.txt" || fail "refused sync still modified files"
st --yes sync --no-snapshot >/dev/null
grep -q "hello v2" "$SYSA/home/user/notes.txt" || fail "sync did not apply edit"

echo "== conflict detection"
st checkout "$SYSA/home/user/app.conf" >/dev/null
echo "config v2-remote" > "$DRIVE/workspace/root$SYSA/home/user/app.conf"
st commit -m "remote config change" >/dev/null
echo "config v2-local" > "$SYSA/home/user/app.conf"
if st --yes sync --no-snapshot >/dev/null 2>&1; then fail "conflict not detected"; fi
grep -q "v2-local" "$SYSA/home/user/app.conf" || fail "conflict clobbered local file"

echo "== forced sync resolves conflict"
st --yes sync --no-snapshot --force >/dev/null
grep -q "v2-remote" "$SYSA/home/user/app.conf" || fail "forced sync did not apply"

echo "== revert notes.txt to first snapshot"
FIRST=$(st list | awk '$3=="system" {print $1; exit}')
st --yes revert "$FIRST" "$SYSA/home/user/notes.txt" >/dev/null
grep -q "hello v1" "$SYSA/home/user/notes.txt" || fail "revert did not restore v1"

echo "== fresh-system restore (machine B)"
mkdir -p "$SYSB"
st --yes --root "$SYSB" restore-full -p home >/dev/null
# restic recreates original absolute paths under --target:
grep -rq "config" "$SYSB$SYSA/home/user/app.conf" || fail "full restore missing files"

echo "== repository verify"
st verify >/dev/null

echo
echo "ALL SMOKE TESTS PASSED  (sandbox: $TMP)"
