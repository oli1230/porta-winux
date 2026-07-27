#!/usr/bin/env bash
# VM test harness for synctool using libvirt + Fedora Cloud + cloud-init.
#
# The "external hard drive" is simulated as a second qcow2 disk (vdb)
# attached to the VM, built from drive-template/ by make-drive-img.sh.
#
# Usage:  vm.sh fetch|drive|up|ssh|push|test|wipe|destroy
#
# Host prerequisites (Fedora): sudo dnf install libvirt virt-install \
#   guestfs-tools qemu-img cloud-utils; systemctl enable --now libvirtd
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
WORK="${SYNCTOOL_VM_DIR:-$HERE/.work}"
VM_NAME="${SYNCTOOL_VM_NAME:-synctool-test}"
FEDORA_VER="${FEDORA_VER:-43}"
IMG_URL="https://download.fedoraproject.org/pub/fedora/linux/releases/${FEDORA_VER}/Cloud/x86_64/images"
BASE_IMG="$WORK/fedora-${FEDORA_VER}-base.qcow2"
OS_DISK="$WORK/${VM_NAME}-os.qcow2"
DRIVE_IMG="$WORK/${VM_NAME}-drive.qcow2"
SEED_ISO="$WORK/${VM_NAME}-seed.iso"
SSH_KEY="$WORK/id_ed25519"

mkdir -p "$WORK"

vm_ip() {
  sudo virsh domifaddr "$VM_NAME" 2>/dev/null \
    | awk '/ipv4/ {sub(/\/.*/, "", $4); print $4; exit}'
}

do_fetch() {
  if [ -f "$BASE_IMG" ]; then echo "base image present: $BASE_IMG"; return; fi
  echo "Fetching Fedora ${FEDORA_VER} Cloud base image…"
  # Filename varies per release; list the dir if this 404s and update.
  local candidates=(
    "Fedora-Cloud-Base-Generic-${FEDORA_VER}-1.x86_64.qcow2"
    "Fedora-Cloud-Base-Generic.x86_64-${FEDORA_VER}-1.qcow2"
  )
  for f in "${candidates[@]}"; do
    if curl -fL --retry 3 -o "$BASE_IMG.tmp" "$IMG_URL/$f"; then
      mv "$BASE_IMG.tmp" "$BASE_IMG"; return
    fi
  done
  echo "Could not guess image filename; browse $IMG_URL and download manually to $BASE_IMG" >&2
  exit 1
}

do_drive() {
  echo "Building fake external drive image from drive-template/…"
  rm -f "$DRIVE_IMG"
  # virt-make-fs packs a directory into a filesystem image without root.
  virt-make-fs --format=qcow2 --type=ext4 --size=+2G \
    "$REPO_ROOT/drive-template" "$DRIVE_IMG"
  echo "drive image: $DRIVE_IMG"
}

do_up() {
  do_fetch
  [ -f "$DRIVE_IMG" ] || do_drive
  [ -f "$SSH_KEY" ] || ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" >/dev/null

  # Fresh overlay OS disk each 'up' → wiping is just deleting this file.
  qemu-img create -f qcow2 -b "$BASE_IMG" -F qcow2 "$OS_DISK" 20G >/dev/null

  sed "s|@PUBKEY@|$(cat "$SSH_KEY.pub")|" "$HERE/cloud-init/user-data.in" \
    > "$WORK/user-data"
  cp "$HERE/cloud-init/meta-data" "$WORK/meta-data"
  cloud-localds "$SEED_ISO" "$WORK/user-data" "$WORK/meta-data"

  sudo virt-install \
    --name "$VM_NAME" \
    --memory 2048 --vcpus 2 \
    --disk "path=$OS_DISK,format=qcow2,bus=virtio" \
    --disk "path=$DRIVE_IMG,format=qcow2,bus=virtio" \
    --disk "path=$SEED_ISO,device=cdrom" \
    --os-variant "fedora-unknown" \
    --import --network network=default --graphics none --noautoconsole
  echo -n "waiting for IP"
  for _ in $(seq 60); do
    ip="$(vm_ip)"; [ -n "$ip" ] && { echo " -> $ip"; break; }
    echo -n "."; sleep 2
  done
  [ -n "$(vm_ip)" ] || { echo "no IP; check 'sudo virsh console $VM_NAME'"; exit 1; }
  echo "ssh with: $0 ssh"
}

do_ssh() {
  exec ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "test@$(vm_ip)" "$@"
}

do_push() {
  # Copy the current source tree into the VM (rsync over ssh).
  rsync -a --delete -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
    --exclude .git --exclude test/vm/.work \
    "$REPO_ROOT/" "test@$(vm_ip):~/synctool/"
}

do_test() {
  do_push
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "test@$(vm_ip)" 'sudo bash ~/synctool/test/vm/in-vm-test.sh'
}

do_wipe() {
  sudo virsh destroy "$VM_NAME" 2>/dev/null || true
  sudo virsh undefine "$VM_NAME" 2>/dev/null || true
  rm -f "$OS_DISK" "$SEED_ISO"
  echo "VM wiped (base image and drive image kept). 'up' recreates it fresh."
}

do_destroy() {
  do_wipe
  rm -rf "$WORK"
  echo "all VM artifacts removed."
}

case "${1:-}" in
  fetch) do_fetch ;;
  drive) do_drive ;;
  up) do_up ;;
  ssh) shift; do_ssh "$@" ;;
  push) do_push ;;
  test) do_test ;;
  wipe) do_wipe ;;
  destroy) do_destroy ;;
  *) grep '^# Usage' "$0" | sed 's/^# //'; exit 1 ;;
esac
