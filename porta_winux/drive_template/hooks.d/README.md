# hooks.d — what hooks are and why they exist

## The concept, from scratch

A **hook** is just a script that porta-winux runs automatically at a fixed
moment in its lifecycle — right before it takes a snapshot, right after it
syncs files to your system, and so on. Each subdirectory here is one of
those moments (an "event"):

| directory        | porta-winux runs its scripts…                           |
|------------------|---------------------------------------------------------|
| `pre-snapshot/`  | just **before** backing up your files                   |
| `post-snapshot/` | just **after** a backup finishes                        |
| `pre-checkout/`  | just **before** extracting files into the workspace     |
| `post-sync/`     | just **after** writing files onto a system (sync, revert, restore) |

That's the whole mechanism. There is no plugin API, no registration, no
config: **a runnable file in one of these folders IS the extension.**
"Runnable" means it starts with a `#!` shebang line (like `#!/bin/bash`)
or is a compiled binary — porta-winux checks this rather than trusting the
executable permission bit, because drives formatted FAT/exFAT/NTFS don't
store Unix permissions and mark *every* file executable. Placeholders
(`.keep`), READMEs, and files without a shebang are always skipped (with a
warning for the latter, since it's probably a hook you forgot the `#!` on).
Scripts in a folder run in filename order (that's why the shipped one is
named `50-restorecon` — you can slot things before or after it with `10-`,
`90-`, etc.). Each script receives context through environment variables:

```
PORTA_WINUX_DRIVE      path to the drive root
PORTA_WINUX_EVENT      which event fired (e.g. "post-sync")
PORTA_WINUX_ROOT       the target system root ("/" normally)
PORTA_WINUX_SNAPSHOT   the relevant snapshot id, when one exists
PORTA_WINUX_PROFILE    the profile name, when relevant
PORTA_WINUX_PATHS      newline-separated list of affected files, when relevant
```

If a `pre-*` script exits non-zero, the operation is **aborted** — pre-hooks
are gatekeepers. A failing `post-*` script prints a warning but doesn't undo
anything — post-hooks are reactions.

## Try it in 30 seconds

```bash
cat > post-snapshot/10-hello <<'EOF'
#!/bin/bash
echo "backup finished! snapshot: $PORTA_WINUX_SNAPSHOT"
EOF
chmod +x post-snapshot/10-hello
porta-winux snapshot        # your message prints after the backup
```

## Why this pattern instead of something simpler?

Fair question — a folder of loose scripts *does* look abstract until you see
what it buys. The alternative would be hard-coding every future feature into
porta-winux's Python: SELinux fixups, security scans, ansible triggers,
special compression… each one a code change, a review, a re-release. With
hooks, each of those is instead **a file you drop on the drive**, in any
language, without touching porta-winux at all. Concretely, from the roadmap:

- SELinux label repair after sync → already here as `post-sync/50-restorecon`
- security/CI scan of each backup → a script in `post-snapshot/`
- pre-compressing formats restic dedups poorly → a script in `pre-snapshot/`
- triggering ansible after a restore → a script in `post-sync/`

The pattern is not something invented for this project — it's how much of
the Linux ecosystem handles extension, so learning it here pays off broadly:

- **git hooks** (`.git/hooks/pre-commit` etc.) are exactly this:
  https://git-scm.com/book/en/v2/Customizing-Git-Git-Hooks
- **`*.d` directories** (`/etc/cron.d`, `/etc/sudoers.d`,
  `/etc/profile.d`…) are the same idea for configuration:
  https://blog.liw.fi/posts/2017/11/20/unix_directories_ending_in_d/
- **systemd drop-ins and run-parts** follow the same convention:
  https://man7.org/linux/man-pages/man8/run-parts.8.html

If you never add a script here, hooks cost you nothing — the folders sit
empty and porta-winux behaves exactly as documented. The `.keep` files only
exist so git/tar preserve the empty directories.

## The one hook that ships by default

`post-sync/50-restorecon` — on Fedora, files carry SELinux security labels,
and files written by porta-winux get generic ones. This script runs
`restorecon` on every file a sync/revert/restore just wrote, resetting the
labels to what Fedora's policy expects. It exits silently on systems
without SELinux. (Background: https://docs.fedoraproject.org/en-US/quick-docs/selinux-getting-started/)
