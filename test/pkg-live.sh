#!/usr/bin/env bash
# LIVE test for a real Fedora machine: runs the pkg scanners against your
# actual dnf/flatpak/rpm and validates that their real output parses.
#
# SAFE BY CONSTRUCTION:
#   - every command it triggers is a read-only query (repoquery/repolist/
#     list/remotes/rpm -Va/rpm -qa)
#   - drafts are written to a THROWAWAY temp directory, not your drive
#   - apply is exercised only as --dry-run (prints commands, executes none)
#   - your real porta-winux drive and packages.toml are never touched
#
# Run it on the desktop first; if it passes, your laptop will behave too.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$REPO_ROOT"

TMP="$(mktemp -d /tmp/porta-winux-live.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
FAKE_DRIVE="$TMP/drive"
mkdir -p "$FAKE_DRIVE"
printf '[porta-winux]\ndefault_profile = "home"\n[profiles.home]\npaths = ["/home"]\n' \
  > "$FAKE_DRIVE/porta-winux.toml"
st() { python3 -m porta_winux.cli --drive "$FAKE_DRIVE" "$@"; }
pyq() { python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT'); $1"; }

echo "=== 1. real scanners parse this machine's tool output ==="
pyq "
from porta_winux import packages as p
pkgs = p.scan_dnf_packages()
assert isinstance(pkgs, list) and all(' ' not in x for x in pkgs), 'dnf parse produced junk'
print(f'  dnf user-installed: {len(pkgs)} packages (sample: {pkgs[:5]})')
repos = p.scan_dnf_repos()
print(f'  non-default repos: {repos}')
try:
    apps = p.scan_flatpak_apps()
    print(f'  flatpak apps: {len(apps)} (sample: {[a[\"id\"] for a in apps[:3]]})')
    print(f'  flatpak remotes: {list(p.scan_flatpak_remotes())}')
except p.ScanUnavailable as e:
    print(f'  flatpak: skipped ({e})')
print(f'  fedora release detected: {p.fedora_release()!r}')
"

echo "=== 2. full scan writes a valid, loadable draft (throwaway drive) ==="
st pkg scan
python3 -c "
import sys, tomllib; sys.path.insert(0,'$REPO_ROOT')
from porta_winux import packages as p
st = p.PackageState.load(__import__('pathlib').Path('$FAKE_DRIVE/packages.draft.toml'))
assert st.dnf_packages, 'draft has no packages — inspect $FAKE_DRIVE/packages.draft.toml'
print(f'  draft valid TOML: {len(st.dnf_packages)} dnf, {len(st.flatpak_apps)} flatpak')
"

echo "=== 3. altered-/etc scan parses real rpm verification ==="
echo "  (takes ~10-60s; run with sudo for a complete list)"
pyq "
from porta_winux import packages as p
etc = p.scan_modified_etc()
sens = [x for x in etc if p.is_sensitive(x)]
print(f'  altered /etc configs: {len(etc)} (sensitive: {len(sens)})')
for x in etc[:10]: print('    ' + x + ('   [SENSITIVE]' if p.is_sensitive(x) else ''))
"

echo "=== 4. adopt + diff + apply --dry-run against the throwaway drive ==="
st --yes pkg adopt >/dev/null
st pkg diff | head -8
st pkg apply --dry-run | tail -5
echo
echo "ALL LIVE CHECKS PASSED — your real drive was never touched."
echo "Next (for real):  porta-winux pkg scan   (against your actual drive)"
