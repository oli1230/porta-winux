# Programs & configs: the pkg subsystem

porta-winux can carry **which programs you chose** and **which config files
you changed** between your machines — recording names and files, never
program binaries. It follows a scan → *you edit* → adopt → diff → apply
lifecycle, and it is **install-only by design**.

## The guarantees (matching how this tool is meant to be used)

1. **Nothing is ever removed.** There is no uninstall code path. Packages
   installed on a machine but absent from the list are *reported*
   ("installed here but not in the list — REPORT ONLY"), never touched.
   As defense in depth, every command `apply` executes passes through a
   guard that refuses removal verbs (`remove`, `erase`, `uninstall`,
   `autoremove`, `purge`); the test suite proves the guard works.
2. **Nothing happens without consent.** `scan`/`scan-configs`/`diff` are
   read-only. `adopt` and each stage of `apply` show a concrete plan and
   ask. No terminal + no `--yes` = refusal, same as the rest of the tool.
3. **One global list.** Desktop and laptop share `packages.toml`. A scan on
   one machine never drops the other machine's designations — entries not
   installed locally are annotated, and only you delete lines.

## Workflow

**On the machine that has your stuff (desktop):**

```bash
porta-winux pkg scan          # read-only; writes <drive>/packages.draft.toml
$EDITOR <drive>/packages.draft.toml    # DELETE lines you don't want synced
porta-winux pkg adopt         # shows +/- vs current list, asks, promotes

porta-winux pkg scan-configs  # finds /etc files you altered (sudo = complete)
$EDITOR <drive>/configs.draft.toml     # uncomment wanted suggestions
porta-winux pkg adopt
sudo porta-winux snapshot -p system-configs    # configs into restic
```

**On the machine that's missing things (laptop / fresh install):**

```bash
porta-winux pkg diff          # report: what would be installed, what's extra
porta-winux pkg apply -n      # dry run: the exact commands, nothing executed
porta-winux pkg apply         # staged, confirmed installs
```

## How the noise problem is solved

You never review 2,500 packages — but there's a Fedora subtlety: dnf's
"user-installed" flag means "not a dependency", and **Anaconda (for example) marks the
entire spin it installs that way** (on live installs, the whole baked-in
package set). So a naive scan shows ~400 packages of spin baseline. Three
mechanisms shrink it, layered from most to least precise:

1. **Real install reasons.** dnf5 records WHY each package is present; the
   scan reads its system-state file (or `repoquery --qf '%{reason}'`) and
   keeps only `reason = User` — your actual requests, not Anaconda's groups.
   The scan prints which strategy it used.
2. **Baseline capture.** On a machine you haven't customized yet (your
   freshly installed laptop is perfect), run
   `porta-winux pkg scan --capture-baseline` once: its package set is saved
   to `baseline.toml` on the drive, and every future scan on any machine
   subtracts it — a literal "diff against a fresh install".
3. **Base-system heuristics in the draft.** Anything that survives but looks
   like spin infrastructure (plasma-*, firmware, grub2-*, spin default apps
   like firefox/dolphin/spectacle…) is moved to a **commented-out** section
   of the draft: nothing is hidden or dropped, and uncommenting a line
   re-designates it. Packages you already designated are never commented.

Flatpak is scanned for **apps only**; runtimes reinstall automatically. Non-default dnf repos (RPM Fusion, COPRs, vendor repos) are
recorded too; `apply` does **not** auto-enable them (repo setup is
repo-specific) — it tells you which are missing and hints how to add them,
then installs from them once you have.

## Configs: how we handle programs configuring themselves differently

Programs store settings in wildly different ways — INI files, JSON, YAML,
sqlite databases (Firefox), a binary database (GNOME's dconf), whole
directories. The design stance: **treat every config as an opaque file and
let restic carry it.** Restic doesn't care about formats, and this folds
configs into the machinery you already trust (snapshots, sync, conflicts,
revert) instead of inventing per-app logic. Two mechanisms feed it:

- **System configs (/etc):** RPM knows the original checksum of every
  config file it shipped, so `rpm` verification yields *exactly* the files
  you've altered — no heuristics. `pkg scan-configs` runs it and drafts the
  list. Files that typically embed secrets (NetworkManager wifi profiles,
  ssh host keys, sssd, PKI) are flagged SENSITIVE and left commented out —
  remember your restic password sits on the drive.
- **User configs (~):** the draft also suggests well-known per-app paths
  for apps you've designated (Firefox profile, VS Code settings, dconf,
  each flatpak's `~/.var/app/<id>` dir), all commented out until you opt in.

Adopted paths become a normal profile named **`system-configs`** — no new
backup machinery, just `snapshot -p system-configs`, and sync/checkout/
revert work on configs like any other files.

Format-specific caveats to know (not handled magically, by choice):

- **dconf** (`~/.config/dconf/user`) is one binary database holding all
  GNOME settings; carrying the file works between similar GNOME versions.
  A cleaner text export (`dconf dump`) is a natural future pre-snapshot hook.
- **sqlite-backed apps** (browsers): snapshot while the app is closed, or
  you may capture a mid-write database.
- Keyrings (`~/.local/share/keyrings`) are deliberately never suggested.

## Fresh-system order of operations

Configs must land **after** their packages (a package install can replace a
pre-existing config with the default). On a new machine:

```bash
porta-winux pkg apply                       # 1. programs
sudo porta-winux restore-full               # 2. your files
sudo porta-winux revert latest \
     /etc/…                                 # 3. (or sync) configs as needed
```

Wiring these into one confirmed `restore-full` orchestration is planned.

## Files on the drive

| file                  | written by       | meaning |
|-----------------------|------------------|---------|
| `packages.draft.toml` | `pkg scan`       | editable candidates; delete = don't sync |
| `packages.toml`       | `pkg adopt`      | the designated global list |
| `configs.draft.toml`  | `pkg scan-configs` | altered /etc files + suggestions |
| `configs.toml`        | `pkg adopt`      | designated config paths → `system-configs` profile |

## Testing

```bash
make pkg-smoke   # anywhere: full flow against FAKE dnf/flatpak/rpm; proves
                 # consent guards and the never-remove invariant end to end
make pkg-live    # on your Fedora machine: read-only scans of the REAL tools,
                 # drafts to a throwaway dir, apply as dry-run only
make unit        # includes parser tests against captured Fedora outputs
```
