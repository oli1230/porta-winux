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
                sync / revert   (restic repo + workspace)
                restore-full
```

## Safety model (read this first)

**porta-winux never writes or deletes data without asking you.** Every
operation that touches a system or removes snapshots (`init-drive`, `sync`,
`revert`, `restore-full`, `prune`) first prints a concrete plan — what,
where — and waits for your explicit yes. Purely additive operations
(`snapshot`, `checkout`, `commit`) don't prompt, because they can't destroy
anything. On top of that, every sync/revert automatically snapshots the
current state of the affected files *first*, so undoing a bad decision is
always one `revert` away. For scripts, kickstart, and CI, `--yes` skips the
prompts; with no terminal attached and no `--yes`, commands refuse rather
than guess.

## Initial setup, step by step

**1. Install the two things it needs** — restic and porta-winux itself:

```bash
sudo dnf install restic          # the backup engine
make install                     # puts the 'porta-winux' command on your PATH
make doctor                      # checks everything is in place, tells you what's optional
```

**2. Plug in your external drive** and note where Fedora mounted it
(usually `/run/media/<you>/<label>` — check with `df` or your file manager).

**3. Turn it into a porta-winux drive:**

```bash
porta-winux init-drive
```

With no path given it lists the mounted drives it can find and lets you
pick from a menu (you can also pass the path directly). It then shows you
exactly what it will create — manifest, empty restic repository, workspace,
hooks — and asks before writing anything.

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
porta-winux snapshot          # backs up the default profile
porta-winux list              # your snapshot, with id and time
porta-winux ls latest         # every file inside it
porta-winux verify            # cryptographic integrity check of the repo
```

That's it — from here on, `porta-winux snapshot` whenever you want a
restore point (or from a cron/systemd timer with `--yes`).

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
porta-winux list                      # history of snapshots and commits
porta-winux revert <snap-id> [paths]  # time-travel (safety snapshot taken first)

# On a brand-new Fedora install:
porta-winux restore-full              # lay down your latest backup
porta-winux prune --keep-last 10      # thin old snapshots (commits always kept)
```

If both a commit and your local machine changed the same file, `sync`
flags a **conflict** and touches nothing — you decide (edit locally and
re-sync, or `sync --force` to take the drive's version).

Every command accepts `--root /some/dir` to operate on a sandbox directory
instead of the real `/` — great for experimenting without risk, and how the
test suite works.

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
`porta-winux --yes --drive /mnt/drive restore-full`.

### Known v1 limitations (deliberate)

- Commits carry adds/edits only — deletions can't be staged yet.
- Conflict resolution is whole-file (keep local or take remote), no merging.
- uid/gid mapping across machines is future work; SELinux labels are
  handled by the shipped restorecon hook.
- One workspace at a time: commit before checking out an unrelated set.

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
