#!/usr/bin/env bash
# Fast smoke test: full snapshot → checkout → commit → sync → conflict →
# restore → cross-machine restore + move/stale-copy detection → mirror →
# journal cycle in a throwaway sandbox. Needs only restic and python3;
# safe to run on any machine (never touches real /).
#
# Two "machines" are simulated with PORTA_WINUX_HOSTNAME (+ separate host
# state dirs) working on the same sandbox paths, because a snapshot's paths
# are absolute and --root only prefixes them at restore time.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

TMP="$(mktemp -d /tmp/porta-winux-smoke.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
DRIVE="$TMP/drive"
SYSA="$TMP/sysA"
SYSB="$TMP/sysB"
export PYTHONPATH="$REPO_ROOT"
export PORTA_WINUX_STATE_DIR="$TMP/stateA"
export PORTA_WINUX_HOSTNAME=machineA

st() { python3 -m porta_winux.cli --drive "$DRIVE" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
sysid() { st list --kind system | awk -v n="$1" 'NR==n+1{print $1}'; }   # nth system snapshot id

echo "== no drive yet: bare invocation is a harmless detect"
if python3 -m porta_winux.cli --drive "$DRIVE" >/dev/null 2>&1; then fail "detect claimed a drive that isn't there"; fi

echo "== init drive"
mkdir -p "$SYSA/home/user/projects/alpha/src" "$SYSA/home/user/projects/keep" "$SYSA/home/user/.cache"
echo "hello v1"  > "$SYSA/home/user/notes.txt"
echo "config v1" > "$SYSA/home/user/app.conf"
echo "print(1)"  > "$SYSA/home/user/projects/alpha/src/main.py"
echo "readme"    > "$SYSA/home/user/projects/alpha/README"
echo "k"         > "$SYSA/home/user/projects/keep/k.txt"
echo "junk"      > "$SYSA/home/user/.cache/junk"
python3 -m porta_winux.cli --yes init drive "$DRIVE" --password smokepass >/dev/null
test -f "$DRIVE/.porta-winux" || fail "init drive wrote no signature"
grep -q drive_id "$DRIVE/.porta-winux" || fail "signature has no drive id"
# scope the sample profile to the sandbox, with an exclude, keeping the fstype pin
python3 - "$DRIVE/porta-winux.toml" "$SYSA" <<'PY'
import re, sys
p, sysa = sys.argv[1], sys.argv[2]
s = open(p).read()
s = re.sub(r'\[profiles\.home\]\npaths = \["/home"\]', f'[profiles.home]\npaths = ["{sysa}/home/user"]', s)
open(p, "w").write(s)
PY
st init detect | grep -q "signature" || fail "init detect did not describe the drive"
st init drive "$DRIVE" --password smokepass >/dev/null <<< "y" 2>/dev/null || true   # idempotent re-run
SIG1=$(grep drive_id "$DRIVE/.porta-winux")

echo "== simulate exFAT drive: mark ALL drive files executable (regression test)"
find "$DRIVE" -type f -exec chmod +x {} +

echo "== system snapshot (machine A) + dry-run preview"
st snapshot -p home -m "A first" >/dev/null
st list | grep -q '"A first"' || fail "journal message not shown in list"
echo "x" >> "$SYSA/home/user/notes.txt"
st snapshot -n | grep -q "~ notes.txt" || fail "snapshot --dry-run did not preview the change"
leaked=$(st snapshot -n | grep -c "junk" || true); [ "$leaked" = 0 ] || fail "excluded .cache leaked into preview"
echo "hello v1" > "$SYSA/home/user/notes.txt"          # back to the snapshot's content

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

echo "== forced sync is journaled with its undo snapshot"
st --yes sync --no-snapshot --force >/dev/null
grep -q "v2-remote" "$SYSA/home/user/app.conf" || fail "forced sync did not apply"
st log --forced | grep -q "FORCED" || fail "forced op missing from log --forced"
UNDO=$(st log --forced | awk '/undo:/{print $4; exit}')
[ -n "$UNDO" ] || fail "no undo snapshot recorded for the forced sync"
st list --forced | grep -q "$UNDO" || fail "list --forced does not show the safety snapshot"
st --yes restore "$UNDO" "$SYSA/home/user/app.conf" >/dev/null
grep -q "v2-local" "$SYSA/home/user/app.conf" || fail "undo via restore <safety> failed"

echo "== partial restore to first snapshot (old 'revert')"
FIRST=$(sysid 1)
st --yes restore "$FIRST" "$SYSA/home/user/notes.txt" >/dev/null
grep -q "hello v1" "$SYSA/home/user/notes.txt" || fail "restore <snap> <path> did not restore v1"
st --yes revert "$FIRST" "$SYSA/home/user/notes.txt" >/dev/null 2>&1 || fail "deprecated revert alias broke"

echo "== move detection: alpha -> beta shows as one directory move"
mv "$SYSA/home/user/projects/alpha" "$SYSA/home/user/projects/beta"
OUT=$(st snapshot -p home -m "moved alpha to beta")
echo "$OUT" | grep -q "> alpha/  -> .*projects/beta" || { echo "$OUT"; fail "directory move not detected at snapshot time"; }
A_MOVED=$(sysid 2)
st compare "$FIRST" "$A_MOVED" --confirm-moves | grep -q "CONFIRMED" || fail "content-hash confirmation of the move failed"

echo "== machine B: has the OLD layout, restores latest from the OTHER machine"
mv "$SYSA/home/user/projects/beta" "$SYSA/home/user/projects/alpha"
echo "mine" > "$SYSA/home/user/only-on-B.txt"
export PORTA_WINUX_HOSTNAME=machineB PORTA_WINUX_STATE_DIR="$TMP/stateB"
if st --yes restore --any-host >/dev/null 2>&1 && false; then :; fi
st --yes restore >/dev/null                      # no args = latest from another host
test -d "$SYSA/home/user/projects/beta" || fail "cross-machine restore did not create beta"
test -d "$SYSA/home/user/projects/alpha" || fail "plain restore must NOT delete the old location"
OUT=$(st snapshot -p home -m "B after restore")
echo "$OUT" | grep -q "= alpha/  identical to .*projects/beta" || { echo "$OUT"; fail "stale copy not flagged as duplicate"; }
echo "$OUT" | grep -q "comparing against .* from machineA" || fail "snapshot did not compare against the restored base"

echo "== restore --mirror removes the stale copy, keeps excluded .cache, previews exactly"
PREV=$(st restore "$A_MOVED" --mirror -n)
echo "$PREV" | grep -q "\- alpha/" || fail "mirror preview missing the stale dir"
echo "$PREV" | grep -q "only-on-B" || fail "mirror preview missing the local-only file"
echo "$PREV" | grep -q "junk" && fail "mirror preview would delete an excluded path"
test -d "$SYSA/home/user/projects/alpha" || fail "dry run deleted something"
st --yes restore "$A_MOVED" --mirror >/dev/null
test ! -e "$SYSA/home/user/projects/alpha" || fail "mirror left the stale copy"
test ! -e "$SYSA/home/user/only-on-B.txt" || fail "mirror left a file not in the snapshot"
test -f "$SYSA/home/user/.cache/junk" || fail "mirror deleted an EXCLUDED path"
test -d "$SYSA/home/user/projects/keep" || fail "mirror deleted a dir that IS in the snapshot"

echo "== compare: no args (latest vs live) and history across 3 snapshots"
st compare | grep -q "change" || fail "compare produced nothing"
st compare "$FIRST" "$A_MOVED" "$(sysid 3)" | grep -q "steps:" || fail "history mode failed"

echo "== fresh-system restore into a sandbox root (machine C, kickstart style)"
mkdir -p "$SYSB"
export PORTA_WINUX_HOSTNAME=machineC PORTA_WINUX_STATE_DIR="$TMP/stateC"
st --yes --root "$SYSB" restore latest -p home --no-preview >/dev/null
# restic recreates original absolute paths under --target:
grep -rq "config" "$SYSB$SYSA/home/user/app.conf" || fail "full restore missing files"
st --yes --root "$SYSB" restore-full -p home >/dev/null 2>&1 || fail "deprecated restore-full alias broke"

echo "== list/log joins, init cleanup, uninstall --host-only"
st list --kind safety | grep -q "before" || fail "safety snapshots not labelled"
st log --cmd restore | grep -q "restore" || fail "log --cmd filter"
st --yes init cleanup >/dev/null
st init uninstall --host-only </dev/null >/dev/null
test ! -e "$TMP/stateC" || fail "uninstall --host-only left host state"
[ "$(grep drive_id "$DRIVE/.porta-winux")" = "$SIG1" ] || fail "drive id changed across re-init"

echo "== repository verify"
st verify >/dev/null

echo
echo "ALL SMOKE TESTS PASSED  (sandbox: $TMP)"
