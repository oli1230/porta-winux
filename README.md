# porta-winux

Back up a Linux system to an external hard drive, browse and **edit** those
backups from any other PC, sync the edits back, revert to any earlier
state, and rebuild a fresh machine from the drive — all on top of a single
deduplicated, encrypted [restic](https://restic.readthedocs.io/) repository.

Think *git semantics over a backup store*: snapshots are your history,
edits made anywhere become commits, and any machine can fast-forward,
revert, or bootstrap itself from scratch.

```
                 snapshot                    checkout
  your machine ─────────────►  external ◄───────────────  any PC
  (Fedora)     ◄─────────────    drive  ───────────────►  (edit, commit)
                sync / restore  (restic repo + workspace)
                compare
```

## Safety model (read this first)

**porta-winux never writes or deletes data without asking you.** Every
operation that touches a system or removes snapshots (`init drive`, `sync`,
`restore`, `prune`) first prints a concrete plan — what, where, and a tree
of the files it would change — and waits for your explicit yes. Purely
additive operations (`snapshot`, `checkout`, `commit`) don't prompt,
because they can't destroy anything. On top of that, every sync/restore
automatically snapshots the current state of the affected files *first*,
so undoing a bad decision is always one `restore <safety-id>` away — and
`porta-winux log` tells you which id, because every write command is
journaled with the snapshots it produced. For scripts, kickstart, and CI,
`--yes` skips the prompts; with no terminal attached and no `--yes`,
commands refuse rather than guess. The only command that deletes files on
your system is `restore --mirror`, and it shows the exact list first.

## Initial setup, step by step

**1. Install the two things it needs** — restic and porta-winux itself:

```bash
sudo dnf install restic          # the backup engine
make install                     # puts the 'porta-winux' command on your PATH
make doctor                      # checks everything is in place, tells you what's optional
```

**2. Plug in your external drive.** Running `porta-winux` with no command
(or `porta-winux init detect`) is always safe: it lists the porta-winux
drives it can see and their state, or tells you nothing is set up yet.

**3. Partition it, then turn it into a porta-winux drive:**

```bash
porta-winux init format /dev/sdX   # once per disk: exFAT shared / btrfs repo / NTFS reserved
porta-winux init drive             # once per repo partition: manifest, signature, restic repo
```

`init format` is destructive and asks you to type the device path; the
reasons for the three-partition layout are in [`STORAGE.md`](STORAGE.md).
`init drive` with no path lists the mounted drives and lets you pick from a
menu (or pass the path). It shows exactly what it will create — the
`.porta-winux` signature that marks the drive, the manifest, an empty
restic repository, the workspace, hooks — and asks before writing anything.
It is idempotent: on an existing drive it only adds what's missing.

**4. Tell it what to back up — this step is yours, don't skip it.**
Open `<drive>/porta-winux.toml` in any editor. The shipped defaults are
just examples. A profile is a name plus the paths you want backed up:

```toml
[porta-winux]
default_profile = "mine"

[profiles.mine]
paths = [
  "/home/you",            # your files
  "/etc/nginx",           # a config you care about
  "/usr/local/bin",       # your scripts
]
excludes = [
  "**/.cache",            # never worth backing up
  "**/node_modules",
  "**/*.iso",             # huge and re-downloadable
]
```

(TOML syntax reference: https://toml.io/en/ — but the pattern above is
really all you need.)

**5. Take your first backup and check it:**

```bash
porta-winux snapshot -m "first"   # backs up the default profile
porta-winux list                  # your snapshot, with id, op id, and your note
porta-winux ls latest             # every file inside it
porta-winux verify                # cryptographic integrity check of the repo
```

That's it — from here on, `porta-winux snapshot` whenever you want a
restore point (or from a cron/systemd timer with `--yes`). Every snapshot
after the first prints a tree of what changed since the last one; add
`-n` to preview without writing.

## Everyday use

```bash
# On any other PC (plug in drive; ./bootstrap.sh on the drive = zero-install):
porta-winux browse                    # TUI: fuzzy-find a file, preview, Enter = edit
porta-winux mount /tmp/snap           # or FUSE-mount everything and use any tool
porta-winux checkout /home/you/.bashrc
$EDITOR <drive>/workspace/root/home/you/.bashrc
porta-winux commit -m "tweak aliases"

# Back on your machine:
porta-winux sync                      # shows the plan, asks, applies commits
porta-winux list                      # snapshots, with the operation that made each
porta-winux log                       # the operations themselves (args, results, undo ids)
porta-winux compare                   # what changed since the last snapshot (tree)
porta-winux restore <snap-id> [paths] # time-travel (safety snapshot taken first)

# Two machines sharing one drive:
porta-winux restore                   # lay down the latest snapshot from the OTHER machine
porta-winux restore --mirror -n       # ...also deleting what it doesn't contain (preview)

# On a brand-new Fedora install:
porta-winux restore latest            # lay down your latest backup, from any machine
porta-winux prune --keep-last 10      # thin old snapshots (commits always kept)

# Done for the day:
porta-winux eject                     # or add --eject to any command
```

If both a commit and your local machine changed the same file, `sync`
flags a **conflict** and touches nothing — you decide (edit locally and
re-sync, or `sync --force` to take the drive's version). Forced operations
are journaled: `porta-winux log --forced` shows each one with the safety
snapshot to restore if you regret it.

Every command accepts `--root /some/dir` to operate on a sandbox directory
instead of the real `/` — great for experimenting without risk, and how the
test suite works.

### Seeing what changed: `compare`

```bash
porta-winux compare                 # latest snapshot (this machine) vs the live system
porta-winux compare a1b2c3          # that snapshot vs the live system
porta-winux compare a1b2c3 d4e5f6   # two snapshots
porta-winux compare a1 b2 c3        # per-step history across a chain of snapshots
porta-winux compare -i              # interactive tree with per-file content diffs
```

Output is a tree rooted at your profile paths, collapsed to `--depth` (2);
`--expand /some/dir` opens one subtree, `--all` everything, `--flat` gives
one line per change for scripts. Legend: `+` added, `-` removed, `~`
modified, `>` moved, `=` duplicate, `T` type change.

Moves are detected by signature (size, mtime, name — what `mv` preserves),
so renaming a directory shows as one `> old/ -> new/` line instead of
hundreds of removes and adds; `--confirm-moves` verifies candidates with
restic's content hashes. A directory marked `=` is an identical copy of a
directory that exists on both sides — what you get when a directory was
moved on machine 1 and then restored onto machine 2, which still had the
old location. `restore --mirror` removes exactly those leftovers (and
anything else under the restored paths that isn't in the snapshot, with
your profile's excludes protected); the preview shows the full delete list
before asking.

### Finding your way back: `list` and `log`

Every write command runs as an *operation* with a short id. Its safety
snapshot and its result snapshot carry that id as a restic tag, and the
drive keeps an append-only journal (`.pw-state/journal.jsonl`) of the
command, its arguments, host, `--force` reason, and free-text `-m` notes.

```bash
porta-winux list --forced           # forced ops and the safety snapshots taken just before
porta-winux list --near a1b2c3      # the snapshots around one
porta-winux log --forced            # each forced op with "undo: porta-winux restore <id>"
porta-winux log --cmd restore -n 5  # the last five restores
```

### Taking it apart

```bash
porta-winux init cleanup            # host state for this drive, stale locks, unadopted drafts
porta-winux init uninstall          # remove porta-winux from the drive and host (typed confirm;
                                    # keeps packages/configs/baseline.toml unless --purge)
porta-winux init uninstall --host-only
```

## What's in this repository

Each folder has its own README explaining its files and the technology
behind them:

| folder | contents |
|--------|----------|
| [`porta_winux/`](porta_winux/README.md) | the Python package — module map and design rules |
| [`porta_winux/drive_template/`](porta_winux/drive_template/README.md) | what ends up on your drive, file by file |
| [`porta_winux/drive_template/hooks.d/`](porta_winux/drive_template/hooks.d/README.md) | **what hooks are, from scratch**, and why they exist |
| [`test/`](test/README.md) | the three test tiers and when to run each |
| [`test/vm/`](test/vm/README.md) | the VM harness: libvirt, cloud-init, the fake external drive |
| [`test/container/`](test/container/README.md) | the fast containerized tier |

## Extending it later (the modularity story)

Three mechanisms carry the roadmap (ansible integration, kickstart,
Windows sync, security scans, compression middleware) without touching the
core:

1. **hooks** — drop a script on the drive, it runs at the right moment.
   Full explanation: [`hooks.d/README.md`](porta_winux/drive_template/hooks.d/README.md).
2. **profiles** in `porta-winux.toml` — new machines or OS slices are just
   more profiles.
3. **`SnapshotStore`** ([`porta_winux/store.py`](porta_winux/README.md)) —
   the only code that knows restic exists; a future borg/kopia backend is
   one class. restic was chosen over borg specifically for native Windows
   support down the road.

Kickstart already works today: in `%post`, mount the drive and run
`porta-winux --yes --drive /mnt/drive restore latest --no-preview`.

### Known limitations (deliberate)

- Commits carry adds/edits only — deletions can't be staged yet.
- Conflict resolution is whole-file (keep local or take remote), no merging.
- uid/gid mapping across machines is future work; SELinux labels are
  handled by the shipped restorecon hook.
- One workspace at a time: commit before checking out an unrelated set.
- Move detection is a heuristic: a file that was moved *and* edited shows
  as removed + added (honest, if less pretty). Snapshot paths are absolute,
  so two machines must keep the same usernames/paths for cross-machine
  restore to line up.
- Retention is `prune --keep-last N` only; restic's time-based policies
  (`--keep-daily/weekly/monthly`) are not exposed yet.

## Upgrading from 0.3

Command names moved; the old ones still work and print a pointer:
`init-drive` → `init drive`, `format-drive` → `init format`,
`revert <snap> [paths]` → `restore <snap> [paths]`, `restore-full` →
`restore [latest]`. Existing drives keep working; run `porta-winux init
drive <path>` once to add the `.porta-winux` signature (nothing else is
touched). The journal starts with the first write after upgrading, so
`log` is empty until then.

## Testing

```bash
make doctor           # dependency check with install hints
make test             # unit tests + full end-to-end cycle in a /tmp sandbox
make container-test   # same cycle in a pristine Fedora 43 container (podman)
make vm-up vm-test    # real Fedora 43 VM with a simulated external drive
make vm-wipe          # instant clean slate for a retest
```

Details and background links in [`test/README.md`](test/README.md).

## Security notes

- The repo password lives on the drive (`.restic-pass`) so foreign PCs work
  with zero setup — which trades away theft protection. The drive's README
  explains the alternative.
- `/etc/shadow*` is excluded by the sample `etc` profile on purpose.
- Syncing/restoring system paths needs `sudo porta-winux …`.
- `restore --mirror` is the only command that deletes files on your
  system. It never uses restic's `restore --delete` (which mirrors the
  whole target directory — with `/` that would be everything); it deletes
  an explicit list computed by `compare`, shown in full before you confirm.
