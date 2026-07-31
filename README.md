# porta-winux

Portable backup/checkout/commit/sync tool for Linux systems, built around a
restic repository living on an external drive. Think *git semantics over a
deduplicated snapshot store*: the drive is the single source of truth, edits
made anywhere become commits, and any machine can fast-forward to them,
revert, or bootstrap itself from scratch.

```
                 snapshot                    checkout
  machine A  ───────────────►  external ◄───────────────  any PC
  (Fedora)   ◄───────────────    drive  ───────────────►  (edit, commit)
                sync / revert   (restic repo + workspace)
                restore-full
```

## Concepts

| porta-winux term      | what it is                                                            |
|--------------------|-----------------------------------------------------------------------|
| **system snapshot**| restic snapshot of a *profile*'s paths taken on a machine (`snapshot`)|
| **profile**        | named path set + excludes in `porta-winux.toml` on the drive             |
| **workspace**      | `drive/workspace/root/` — editable mirror-by-absolute-path area       |
| **commit**         | snapshot of workspace edits, with a message (`commit -m …`)           |
| **sync**           | apply pending commits to a live system, with conflict detection       |
| **base**           | the system snapshot a host was last known to match (drives conflicts) |
| **safety snapshot**| automatic pre-sync/pre-revert snapshot — undo is always available     |

Storage stays minimal because everything — system snapshots, commits, safety
snapshots — deduplicates against everything else in one restic repo.

## Quickstart

```bash
make install                      # or: pip install --user .
porta-winux init-drive /run/media/$USER/MYDRIVE
$EDITOR /run/media/$USER/MYDRIVE/porta-winux.toml   # define profiles
porta-winux snapshot                 # back up the default profile
```

The drive is auto-detected under `/run/media/$USER`, `/media`, `/mnt`
(whichever mount contains a `porta-winux.toml`); override with `--drive` or
`PORTA_WINUX_DRIVE`.

On a foreign PC (nothing installed): plug in the drive and run
`./bootstrap.sh` from its root — it uses the static restic in `drive/bin/`
and the source copy in `drive/porta-winux-src/` if present.

```bash
# browse / edit on any PC
porta-winux browse                       # Textual TUI: fuzzy filter, preview, Enter=edit
porta-winux ls latest | grep bashrc      # or plain unix
porta-winux mount /tmp/snap              # or FUSE-mount everything, use any tool
porta-winux checkout /home/me/.bashrc    # stage into workspace
$EDITOR <drive>/workspace/root/home/me/.bashrc
porta-winux commit -m "tweak aliases"

# back on the original machine
porta-winux sync                         # applies commits; conflicts are flagged, never clobbered
porta-winux sync --force                 # ...unless you say so
porta-winux list                         # history
porta-winux revert <snap-id> [paths…]    # time-travel (auto safety snapshot first)

# fresh Fedora install
porta-winux restore-full                 # lay down the latest system snapshot
porta-winux verify                       # restic integrity check
porta-winux prune --keep-last 10         # thin old system/safety snapshots; commits kept
```

Every command accepts `--root /some/dir` to operate on a sandbox instead of
the real `/` — that's how the tests run, and it's handy for dry experiments.

## Conflict model

For each file a commit wants to write:

- local content == incoming → already applied, skip
- local missing, or local == the host's **base** snapshot → safe, write
- otherwise → **conflict**: the file changed locally since the last sync.
  Nothing is written; resolve locally (or `--force`) and re-run.

A safety snapshot of every affected file is taken before any sync/revert
writes, so recovery from a bad sync is `porta-winux revert <safety-id>`.

## Extending (the modularity story)

Three mechanisms carry the roadmap without touching the core:

1. **hooks.d/** — drop an executable in
   `hooks.d/{pre-snapshot,post-snapshot,pre-checkout,post-sync}/`.
   Context arrives as `PORTA_WINUX_*` env vars (see `src/porta_winux/hooks.py`).
   A Fedora `restorecon` post-sync hook ships in the drive template.
   *Future homes:* security scans (post-snapshot), compression middleware
   for hostile file types (pre-snapshot), ansible trigger (post-sync).
2. **profiles** in `porta-winux.toml` — new machines, OS slices, or a future
   Windows path set are just more profiles.
3. **`SnapshotStore`** (`src/porta_winux/store.py`) — the only file that knows
   restic exists. Borg/kopia/whatever later = implement this one class.
   restic was chosen over borg specifically for native Windows support.

Kickstart integration is already possible: in `%post`, mount the drive and
run `porta-winux --drive /mnt/drive restore-full`.

### Known v1 limitations (deliberate)

- Commits carry file adds/edits only — **deletions** can't be expressed via
  the workspace yet (planned: a `porta-winux rm` staging command).
- Conflict resolution is file-level accept-local/accept-remote; no merging.
- Ownership/mode of synced files follows the committing user; SELinux
  contexts are fixed by the restorecon hook, uid/gid mapping is future work.
- One workspace at a time; commit before checking out an unrelated set.

## Testing

Two tiers, per the design discussion:

```bash
make doctor           # verify required/optional dependencies
make test             # unit tests + full e2e smoke cycle in a sandbox (needs restic)
make container-test   # same, inside a clean Fedora 43 podman container
make vm-up            # libvirt VM from Fedora Cloud image + cloud-init,
                      #   with the "external drive" attached as a 2nd qcow2 disk
make vm-test          # rsync source in, run smoke + VM-only tests (auto-detect, hooks, SELinux)
make vm-wipe          # delete VM + OS overlay disk; base image kept → instant clean retest
```

VM prerequisites (host): `sudo dnf install libvirt virt-install guestfs-tools
qemu-img cloud-utils && sudo systemctl enable --now libvirtd`.

`pytest` runs the pure-logic unit tests in `tests/`.

## Layout

```
├── Makefile
├── src/porta_winux/
│   ├── store.py       SnapshotStore abstraction + ResticStore backend
│   ├── manifest.py    drive discovery, porta-winux.toml profiles
│   ├── workspace.py   checkout / commit / status
│   ├── sync.py        host state, sync engine, revert, restore-full
│   ├── hooks.py       hooks.d runner
│   ├── browse.py      Textual TUI (optional: pip install '.[tui]')
│   └── cli.py         thin argparse layer over the library
│   ├── drive_template/  copied onto new drives by init-drive (packaged)
├── test/
│   ├── smoke.sh       fast e2e in a sandbox
│   ├── container/     Fedora 43 Containerfile
│   └── vm/            vm.sh lifecycle + cloud-init + in-vm-test.sh
└── tests/             pytest unit tests
```

## Security notes

- `init-drive` writes the repo password to `.restic-pass` on the drive so a
  foreign PC works with zero setup. That trades away theft protection — see
  `the drive's README.md` for the alternative.
- `/etc/shadow*` is excluded by the default `etc` profile on purpose.
- Sync/restore write with the invoking user's privileges; system paths need
  `sudo porta-winux …` (state then lives under root's `~/.local/state`).
