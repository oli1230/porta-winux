# porta_winux/ — the Python package

Every command in the CLI is a thin wrapper over functions in these modules,
so future interfaces (kickstart, CI, a GUI) reuse the same code paths.

| file           | responsibility |
|----------------|----------------|
| `cli.py`       | argument parsing and the confirmation flow; calls the library, prints results. Start reading here. |
| `store.py`     | `SnapshotStore`: the **only** file that knows restic exists. Defines the abstract operations (backup, restore, diff, dump…) and implements them by shelling out to restic with JSON output. Swapping to borg/kopia later = reimplementing this one class. |
| `manifest.py`  | finds the drive (a directory containing `porta-winux.toml`) and parses that manifest into profiles (named path sets + excludes). |
| `workspace.py` | checkout (extract files from a snapshot into `drive/workspace/root/`) and commit (snapshot those edits back with a message). Commit metadata travels *inside* the snapshot as `/.porta-winux-commit.json`, so it survives repo copies and different mount points. |
| `sync.py`      | the sync engine: per-host state, the three-way conflict rule, revert, and full restore. Also takes the automatic safety snapshots. |
| `interact.py`  | the human-in-the-loop layer: confirmation prompts and pickers used before any operation that writes or deletes data (`--yes` bypasses, no-TTY refuses). |
| `hooks.py`     | runs the executables in `drive/hooks.d/<event>/` — see `drive_template/hooks.d/README.md` for what hooks are. |
| `browse.py`    | the interactive TUI (fuzzy filter → preview → edit). Optional; needs `pip install '.[tui]'`. |
| `drive_template/` | the files copied onto a new drive by `init-drive` (packaged here so pip installs work). |

## Key design rules

1. **Nothing outside `store.py` calls restic.** Keeps the backend swappable.
2. **Nothing writes to a system or deletes data without going through
   `interact.confirm()`.** Additive operations (snapshot, checkout, commit)
   are exempt — they can't destroy anything.
3. **Every path-writing function takes `root=`** so tests can operate on a
   sandbox directory instead of the real `/`.

## Technology background

- restic (the snapshot engine underneath everything):
  docs https://restic.readthedocs.io/ — especially "References > Design"
  for how content-defined chunking gives us cheap deduplicated snapshots.
- TOML (the manifest format): https://toml.io/en/
- Textual (the TUI framework for `browse`): https://textual.textualize.io/
- Python subprocess + JSON lines (how we drive restic):
  https://docs.python.org/3/library/subprocess.html
