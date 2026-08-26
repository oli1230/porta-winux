# porta-winux storage layer design

## Background: why this exists

A restic repository on `OliDrive` accumulated silent corruption: 13 of 48 pack
files (~27%) failed `restic check --read-data`. Independent SHA-256 verification
of pack filenames against contents reproduced all 13 mismatches exactly, proving
the corruption was deterministic and stored at rest rather than a flaky read.

A 4 GiB sequential write/verify test on the same drive passed cleanly, and dmesg
showed no USB link resets or UAS errors. So the drive hardware is fine. The
failure was in the software write path.

Two contributing causes were identified:

1. **ntfs-3g (FUSE userspace NTFS).** restic uploads pack files from multiple
   goroutines concurrently — each writes a temp file, fsyncs, then renames it
   into `data/xx/`. That is many concurrent small-file writes plus renames,
   nothing like the single sequential stream that passed the `dd` test.
   ntfs-3g is weakest exactly there.

2. **Windows Fast Startup / hibernation.** The drive was connected while the
   machine booted into the Windows partition on the same desktop. A hibernated
   Windows resuming with a stale in-memory view of an NTFS volume flushes cached
   clusters over data written by Linux in the meantime, silently reverting or
   garbling a scattered subset of files with no I/O errors reported anywhere.

Cause 2 is why simply switching to the kernel `ntfs3` driver is not sufficient.
It would improve the write path but leaves the volume mountable by Windows, so
the hibernation-flush hazard remains.


## The fix

Put the Linux repo on a filesystem **Windows cannot mount**. This converts
cause 2 from "mitigated by user discipline" into "structurally impossible".


## Partition layout (GPT)

| # | Label       | Filesystem | Type GUID | Windows sees          | Purpose                                    |
|---|-------------|------------|-----------|-----------------------|--------------------------------------------|
| 1 | `PW_SHARED` | exFAT      | `0700`    | Drive letter          | Human files, docs, portable binaries       |
| 2 | `PW_LINUX`  | btrfs      | `8300`    | Nothing (no letter)   | Linux restic repo + Linux sync state       |
| 3 | `PW_WIN`    | NTFS       | `0700`    | Drive letter          | Future Windows-to-Windows restic repo      |

### Why the type GUIDs matter

Windows assigns drive letters only to partitions tagged *Microsoft basic data*
(`0700`). Tagging partition 2 as *Linux filesystem* (`8300`,
`0FC63DAF-8483-4772-8E79-3D69D8477DE4`) means Windows will not letter it, will
not mount it, and — critically — will **not** show the "You need to format the
disk in drive X: before you can use it" dialog. That dialog is the single most
common way a Linux partition on a shared removable drive gets destroyed.

Partition 1 is deliberately first and exFAT so that *something* useful and
familiar appears when the drive is plugged into any machine, including one
running an OS neither of us planned for.

### Never install WinBtrfs

Third-party btrfs drivers for Windows exist. Installing one defeats the entire
point of this design. Don't.


## Filesystem choices and tradeoffs

| Option | Survives unclean eject | Data checksums | Windows access | Verdict |
|---|---|---|---|---|
| **btrfs** (chosen) | Good — CoW, rolls back to last committed transaction | **Yes**, self-heals with `-d dup` | None (by design) | Recommended |
| ext4 | Good — metadata journal | No | Read-only via WSL2 `wsl --mount`, or paid drivers | Conservative fallback |
| ntfs3 (kernel) | Fair | No | Full native | Rejected: Windows can still clobber it |
| ntfs-3g (FUSE) | Poor | No | Full native | **Rejected: this is the current bug** |
| exFAT | Poor — no journal | No | Full native | Fine for scratch, unfit for a repo |
| FS-in-a-file container | Poor — two layers to corrupt | Depends | Opaque | Rejected: worse failure modes |

btrfs wins on the one property that would have caught this problem on day one:
it verifies a checksum on every block read and raises an I/O error rather than
returning silently wrong bytes. Ext4 would have failed just as quietly as NTFS
did. On a single device, `mkfs.btrfs -d dup -m dup` stores two copies of
everything so btrfs can repair a bad block rather than merely report it. At
roughly 700 MB of repo on a multi-hundred-gigabyte drive, the 2x cost is
irrelevant.

Caveat: on some SSDs the controller may internally deduplicate the two `dup`
copies, weakening the guarantee. Consumer drives like the T7 generally do not,
and even in the worst case you retain full corruption *detection*, which is the
primary win.

To use ext4 instead, set `PW_FSTYPE=ext4` before running `pw-drive-init`, and
change `PW_EXPECTED_FSTYPE` in `pw.conf` to match.


## How this is wired into porta-winux

(The original response to this incident was a standalone pw-backup/pw-verify
toolset. It was integrated INTO porta-winux instead, so that every write path
— snapshot, commit, sync, revert, restore-full, prune — inherits the guards,
not just a parallel backup command. Deltas from the standalone design: the
tool accepts whatever mount point the desktop chose and asserts the FILESYSTEM
rather than fighting the automounter; btrfs defaults to `-m dup` (metadata
duplicated) with `--dup-data` opt-in instead of halving capacity by default;
and the repo password stays on the drive per the project's documented
zero-install tradeoff. The udev rule survives as an optional extra in setup/.)

### The regression guard
`[storage] expected_fstype` is pinned in porta-winux.toml by init-drive.
Every write refuses if the drive is mounted as anything else, with a specific
lecture for fuseblk/ntfs-3g. On unpinned drives, risky filesystems (ntfs*,
exfat, vfat, fuseblk) draw a loud warning.

### The clean-marker protocol
Every write operation consumes `.pw-state/clean` at start and rewrites it on
success. Marker absent = the last session didn't finish (yank, crash), and
all writes refuse until `porta-winux verify` passes. A FAILED verify marks
the drive dirty, so corruption also blocks writes. `porta-winux eject`
syncs, writes the marker, unmounts, powers off, and prints SAFE TO UNPLUG.

### Verification tiers
`porta-winux verify` = tier 1 (every pack file hashed against its filename —
seconds, no password needed, the check that would have caught this incident
on day one) + restic check. `verify --deep` adds --read-data; run it after
any unsafe unplug. On btrfs, --deep also prints the scrub command.

### Formatting a drive
`porta-winux format-drive /dev/sdX` creates the three-partition layout above.
It is deliberately interactive-only: --yes is NOT honored, and confirmation
is typing the device path. `--dry-run` prints the exact commands.
`--fstype ext4`, `--dup-data`, `--no-win`, `--shared`/`--linux` adjust it.

### Routine
    plug in -> work (snapshot/sync/...) -> porta-winux eject -> unplug
    weekly or after any doubt:  porta-winux verify --deep

## Windows notes (future)
- The Windows repo goes on PW_WIN; Linux never mounts it (if it ever must:
  kernel ntfs3, read-only — never ntfs-3g).
- On any Windows host that touches the drive: `powercfg /h off` (kills Fast
  Startup + hibernation), Quick-removal policy, Safely Remove Hardware.
- PW_SHARED is exFAT and mountable by Windows, so the hibernation hazard
  still applies TO IT — treat it as expendable scratch, never repo storage.
- Never install WinBtrfs; it defeats the entire design.
