# test/vm/ — full virtual machine testing

This tier boots a *real* Fedora 43 in a VM, attaches a simulated external
hard drive, and runs the end-to-end tests plus VM-only checks (SELinux
relabeling via the restorecon hook, drive auto-detection at
/run/media/..., hooks firing).

## Files
- `vm.sh` — the whole lifecycle: `fetch` (download the Fedora Cloud
  image), `drive` (pack drive_template/ into a qcow2 disk image), `up`
  (create + boot the VM), `ssh`, `push` (rsync source in), `test`,
  `wipe` (delete the VM for a clean retest; base image kept), `destroy`.
- `cloud-init/` — first-boot config: creates a `test` user with your
  generated SSH key, installs restic, and mounts the fake drive where a
  real removable drive would appear.
- `in-vm-test.sh` — what `make vm-test` executes inside the VM.

## How the pieces work (with docs)
- **libvirt/virt-install** manage the VM:
  https://libvirt.org/ · https://virt-manager.org/
- **Fedora Cloud images** are pre-installed qcow2 disks — booting one
  takes seconds versus a 10-minute anaconda install:
  https://fedoraproject.org/cloud/
- **cloud-init** configures a cloud image on first boot (users, keys,
  packages): https://cloudinit.readthedocs.io/
- **qcow2 overlays**: the VM's OS disk is a copy-on-write overlay on the
  base image, so `wipe` = delete one file, and a fresh system is one
  `make vm-up` away: https://www.qemu.org/docs/master/system/images.html
- **virt-make-fs** packs a directory into a filesystem image without
  root — how the fake external drive is built:
  https://libguestfs.org/virt-make-fs.1.html

## The external drive trick
No USB passthrough needed: the drive is just a second virtual disk
(`/dev/vdb`) built from `porta_winux/drive_template/`. cloud-init mounts
it at `/run/media/test/SYNCDRIVE`, exactly where Fedora would mount a
real plugged-in drive, so auto-detection is tested for real.

Host setup (once):
  sudo dnf install libvirt virt-install guestfs-tools qemu-img cloud-utils
  sudo systemctl enable --now libvirtd
