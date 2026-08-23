# the porta-winux drive

This directory tree is copied onto a new external drive by
`porta-winux init-drive` (which shows you a plan and asks first).

| entry | what it is |
|-------|-----------|
| `porta-winux.toml` | **the manifest — the file you edit.** Profiles = named lists of paths to back up, with excludes. Its presence is also what marks a directory as a porta-winux drive. TOML syntax: https://toml.io/en/ |
| `repo/`            | the restic repository holding every snapshot, deduplicated and encrypted. Never edit by hand. https://restic.readthedocs.io/ |
| `.restic-pass`     | the repo password (mode 600). See security note below. |
| `workspace/root/`  | the editable area: `checkout` puts files here mirrored by absolute path (`/home/you/x` → `workspace/root/home/you/x`), you edit them, `commit` snapshots them back. |
| `hooks.d/`         | drop-in extension scripts — **see hooks.d/README.md for a full explanation of what hooks are.** |
| `bin/`             | put a static `restic` binary here and foreign PCs need nothing installed. |
| `bootstrap.sh`     | run from the drive root on any Linux PC for a zero-install entry point. |

## Security note
Keeping `.restic-pass` next to the repo makes the drive fully self-contained,
but it means the encryption does not protect you if the drive is stolen.
If that matters to you: delete `.restic-pass`, store the password in a
password manager, and export `RESTIC_PASSWORD` when using the drive.
