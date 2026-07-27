# synctool drive

This directory tree is copied onto a new external drive by
`synctool init-drive /path/to/mounted/drive`.

- `synctool.toml`  — what to back up (profiles). Edit freely.
- `repo/`          — restic repository. Never touch by hand.
- `.restic-pass`   — repo password. See security note below.
- `workspace/root/`— checked-out files land here, mirrored by absolute path.
- `hooks.d/`       — drop executables here to extend synctool (see main README).
- `bin/`           — optional static `restic` so foreign PCs need no installs.
- `bootstrap.sh`   — run this on any PC for a zero-install entry point.

## Security note
Keeping `.restic-pass` next to the repo makes the drive self-contained but
means encryption does not protect against drive theft. If that matters,
delete `.restic-pass` and supply the password via RESTIC_PASSWORD or a
password manager instead.
