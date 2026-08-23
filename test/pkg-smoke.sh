#!/usr/bin/env bash
# End-to-end test of the pkg subsystem using FAKE dnf/flatpak/rpm/sudo
# binaries, so the complete scan -> edit -> adopt -> diff -> apply flow runs
# on any machine, with no root and no real package operations. Also proves
# the never-remove guarantee by logging every command "executed".
# Needs: python3, restic (for the configs-snapshot leg).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

TMP="$(mktemp -d /tmp/porta-winux-pkg.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
DRIVE="$TMP/drive"; FAKE="$TMP/fakebin"; STATE="$TMP/fakestate"; SB="$TMP/sandbox"
LOG="$TMP/cmd.log"
mkdir -p "$FAKE" "$STATE" "$SB/etc/ssh"
export PYTHONPATH="$REPO_ROOT" PORTA_WINUX_STATE_DIR="$TMP/pwstate"
export PORTA_WINUX_DNF5_STATE="/nonexistent"
st() { python3 -m porta_winux.cli --drive "$DRIVE" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# ---- fake package managers ---------------------------------------------------
# State files: rpms.txt (installed rpm names), userinstalled.txt, flatpaks.txt
printf 'htop\ngit\nbash\nglibc\n' > "$STATE/rpms.txt"          # bash/glibc = deps
printf 'htop\ngit\n'              > "$STATE/userinstalled.txt" # user-chosen only
printf 'org.gimp.GIMP\tflathub\n' > "$STATE/flatpaks.txt"

cat > "$FAKE/dnf" <<EOF
#!/usr/bin/env bash
echo "dnf \$*" >> "$LOG"
case "\$1 \$2" in
  "repoquery --installed")
     # name<TAB>reason for every installed package, padded past the parser's
     # 100-line sanity floor (real rpm databases are always bigger than that)
     while read -r p; do
       if grep -qx "\$p" "$STATE/userinstalled.txt"; then echo -e "\$p\tUser";
       else echo -e "\$p\tDependency"; fi
     done < "$STATE/rpms.txt"
     for i in \$(seq 1 120); do echo -e "fakedep\$i\tDependency"; done ;;
  "repoquery --userinstalled") cat "$STATE/userinstalled.txt" ;;
  "repolist --enabled") printf 'repo id repo name\nfedora Fedora\nupdates Updates\nrpmfusion-free RPM Fusion\n' ;;
  "install -y")
     shift 2
     for p in "\$@"; do case "\$p" in --*) ;; *) echo "\$p" >> "$STATE/rpms.txt";; esac; done ;;
  *) exit 0 ;;
esac
EOF
cat > "$FAKE/rpm" <<EOF
#!/usr/bin/env bash
echo "rpm \$*" >> "$LOG"
if [ "\$1" = "-qa" ]; then cat "$STATE/rpms.txt"
elif [ "\$1" = "-Va" ]; then
  printf 'S.5....T.  c /etc/ssh/sshd_config\n.M.......  c /etc/dnf/dnf.conf\n'
  exit 1   # rpm exits 1 when verification finds differences
fi
EOF
cat > "$FAKE/flatpak" <<EOF
#!/usr/bin/env bash
echo "flatpak \$*" >> "$LOG"
case "\$1" in
  list) cat "$STATE/flatpaks.txt" ;;
  remotes) printf 'flathub\thttps://dl.flathub.org/repo/\n' ;;
  install) app="\${@: -1}"; printf '%s\tflathub\n' "\$app" >> "$STATE/flatpaks.txt" ;;
  remote-add) : ;;
esac
EOF
cat > "$FAKE/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod +x "$FAKE"/*
export PATH="$FAKE:$PATH"

echo "== init drive"
python3 -m porta_winux.cli --yes init-drive "$DRIVE" --password pkgtest >/dev/null

echo "== machine A: pkg scan writes an annotated draft (and only the draft)"
st pkg scan > "$TMP/scan.out"
grep -q "2 user-installed dnf" "$TMP/scan.out" || fail "userinstalled filter wrong"
grep -q "install reasons via repoquery" "$TMP/scan.out" || fail "precise reason strategy not used"
test -f "$DRIVE/packages.draft.toml" || fail "no draft written"
test ! -f "$DRIVE/packages.toml" || fail "scan must not create packages.toml"
grep -q '"bash"' "$DRIVE/packages.draft.toml" && fail "dependency leaked into draft"
grep -q "rpmfusion-free" "$DRIVE/packages.draft.toml" || fail "non-default repo missed"

echo "== user edits draft: designate htop + gimp, reject git"
sed -i '/"git"/d' "$DRIVE/packages.draft.toml"
echo "== consent guard: adopt without --yes and no TTY must refuse"
if st pkg adopt >/dev/null 2>&1; then fail "adopt proceeded without consent"; fi
test ! -f "$DRIVE/packages.toml" || fail "refused adopt still wrote packages.toml"
st --yes pkg adopt | grep -q "adopted packages.toml" || fail "adopt failed"

echo "== machine A is complete: diff shows git as local-extra, never removed"
st pkg diff | grep -q "dnf to install: 0" || fail "unexpected installs on machine A"
st pkg diff | grep -q "    git" || fail "local extra not reported"

echo "== simulate MACHINE B (fresh laptop): fewer packages installed"
printf 'bash\nglibc\n' > "$STATE/rpms.txt"
printf '' > "$STATE/userinstalled.txt"
printf '' > "$STATE/flatpaks.txt"

st pkg diff | grep -q "dnf to install: 1" || fail "diff missed missing package"
echo "== apply consent guard: no TTY + no --yes must refuse before installing"
: > "$LOG"
if st pkg apply >/dev/null 2>&1; then fail "apply proceeded without consent"; fi
grep -Eq "dnf install|flatpak install|remote-add" "$LOG" && fail "refused apply still ran package commands"

echo "== dry-run executes nothing"
st pkg apply --dry-run | grep -q "DRY-RUN" || fail "dry-run not shown"
grep -q "dnf install" "$LOG" && fail "dry-run executed dnf"

echo "== real apply (fake managers): installs only, then converges"
st --yes pkg apply >/dev/null || fail "apply failed"
grep -q "htop" "$STATE/rpms.txt" || fail "htop not installed"
grep -q "org.gimp.GIMP" "$STATE/flatpaks.txt" || fail "flatpak not installed"
st --yes pkg apply | grep -q "nothing to install" || fail "apply did not converge"

echo "== NEVER-REMOVE: no removal verb ever reached a package manager"
if grep -Ei "remove|erase|uninstall|autoremove|purge" "$LOG"; then
  fail "a removal verb was executed!"
fi

echo "== configs: scan -> adopt -> becomes profile -> restic snapshot works"
st pkg scan-configs >/dev/null
grep -q "sshd_config" "$DRIVE/configs.draft.toml" || fail "altered config missed"
grep -q "dnf.conf" "$DRIVE/configs.draft.toml" && fail "mode-only change included"
st --yes pkg adopt >/dev/null
echo "PermitRootLogin no" > "$SB/etc/ssh/sshd_config"
st --root "$SB" snapshot -p system-configs >/dev/null || fail "configs snapshot failed"
st ls latest | grep -q "sshd_config" || fail "config file not in snapshot"

echo
echo "ALL PKG SMOKE TESTS PASSED  (sandbox: $TMP)"
