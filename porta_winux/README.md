# porta_winux/ — the Python package

Every command in the CLI is a thin wrapper over functions in these modules,
so future interfaces (kickstart, CI, a GUI) reuse the same code paths.

| file           | responsibility |
|----------------|----------------|
| `cli.py`       | argument parsing and the confirmation flow; calls the library, prints results. Start reading here. |
| `store.py`     | `SnapshotStore`: the **only** file that knows restic exists. Defines the abstract operations (backup, restore, diff, dump…) and implements them by shelling out to restic with JSON output. Swapping to borg/kopia later = reimplementing this one class. |
| `manifest.py`  | finds the drive (a directory carrying the `.porta-winux` signature, or just a manifest on pre-0.4 drives), the signature helpers, and manifest parsing into profiles (named path sets + excludes). |
| `workspace.py` | checkout (extract files from a snapshot into `drive/workspace/root/`) and commit (snapshot those edits back with a message). Commit metadata travels *inside* the snapshot as `/.porta-winux-commit.json`, so it survives repo copies and different mount points. |
| `sync.py`      | the sync engine: per-host state, the three-way conflict rule, and `restore` (partial or whole, other-host default, `--mirror` with an explicit delete list). Also takes the automatic safety snapshots. |
| `compare.py`   | the comparison engine: listings from a snapshot (`restic ls`) or the live filesystem, diff with move/duplicate detection, the collapsible tree renderer, multi-snapshot history. Used by `compare`, by `snapshot`'s change summary, and by `restore`'s preview/delete list. |
| `compare_tui.py` | the interactive tree for `compare -i` (Textual, optional). |
| `ops.py`       | operation ids, the `op:`/`via:`/`pre:`/`forced` tag scheme, and the append-only journal `.pw-state/journal.jsonl` behind `log` and the extra columns of `list`. |
| `drivesetup.py` | the `init` group: `init drive` (idempotent furnishing + signature), `detect`, `cleanup`, `uninstall`. |
| `interact.py`  | the human-in-the-loop layer: confirmation prompts and pickers used before any operation that writes or deletes data (`--yes` bypasses, no-TTY refuses). |
| `hooks.py`     | runs the executables in `drive/hooks.d/<event>/` — see `drive_template/hooks.d/README.md` for what hooks are. |
| `browse.py`    | the interactive TUI (fuzzy filter → preview → edit). Optional; needs `pip install '.[tui]'`. |
| `drivehealth.py` | the storage-integrity layer (fs pin, clean marker, pack hashing, eject, formatting) — see STORAGE.md. |
| `drive_template/` | the files copied onto a new drive by `init drive` (packaged here so pip installs work). |

## Key design rules

1. **Nothing outside `store.py` calls restic.** Keeps the backend swappable.
2. **Nothing writes to a system or deletes data without going through
   `interact.confirm()`.** Additive operations (snapshot, checkout, commit)
   are exempt — they can't destroy anything.
3. **Every path-writing function takes `root=`** so tests can operate on a
   sandbox directory instead of the real `/`.
4. **Every write command runs inside an `ops.Operation`** so its safety and
   result snapshots share an op id and the journal can answer "what happened
   and how do I undo it".
5. **Nothing deletes on the live system except `sync.restore(mirror=True)`**,
   which removes an explicit, previewed list. restic's own `restore --delete`
   is intentionally not exposed (it mirrors the whole `--target`).

## Technology background

- restic (the snapshot engine underneath everything):
  docs https://restic.readthedocs.io/ — especially "References > Design"
  for how content-defined chunking gives us cheap deduplicated snapshots.
- TOML (the manifest format): https://toml.io/en/
- Textual (the TUI framework for `browse`): https://textual.textualize.io/
- Python subprocess + JSON lines (how we drive restic):
  https://docs.python.org/3/library/subprocess.html
