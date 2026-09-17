"""porta-winux CLI. Thin layer over the library; kickstart, CI, and future
GUIs should call the same library functions these commands do."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import PortaWinuxError, __version__, drivehealth, drivesetup, hostname, interact, ops
from . import compare as cmp
from . import packages as pkgmod
from . import sync as syncmod
from . import workspace as ws
from .manifest import MANIFEST_NAME, DriveLayout, find_drive
from .store import ResticStore, Snapshot


def _layout_store(args) -> tuple[DriveLayout, ResticStore]:
    layout = find_drive(args.drive)
    return layout, ResticStore(layout)


# -- commands ----------------------------------------------------------------


def _profile_roots(manifest, prof_name: str | None) -> list[str]:
    try:
        return list(manifest.profile(prof_name).paths)
    except PortaWinuxError:
        return []


def _print_change_tree(changes, roots, depth, expand=None, show_all=False, header=None):
    if header:
        print(header)
    if not changes:
        print("  no changes")
        return
    tree = cmp.build_tree(changes, roots)
    print(cmp.render_tree(tree, depth=depth, expand=expand, show_all=show_all))


def cmd_snapshot(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    prof = manifest.profile(args.profile)
    me = hostname()
    # What to compare against: this host's recorded base (its own last
    # snapshot, or whatever it last restored/synced to), else the newest
    # snapshot of the profile from this host.
    all_system = store.snapshots(tags=["system"])
    state = syncmod.HostState.load(store.repo_id())
    parent = next((s for s in all_system if s.id == state.base_snapshot), None)
    if parent is None:
        prev = [s for s in all_system if f"profile:{prof.name}" in s.tags and s.hostname == me]
        parent = prev[-1] if prev else None

    if args.dry_run:
        # Preview: live filesystem vs this host's last snapshot of the profile.
        if not parent:
            print(f"no previous '{prof.name}' snapshot from {me}: the first snapshot "
                  f"will contain everything under {', '.join(prof.paths)}")
            return 0
        a = cmp.listing_from_snapshot(store, parent.id, under=parent.paths)
        b = cmp.listing_from_live(prof.paths, prof.excludes, root=args.root)
        changes = cmp.diff_listings(a, b, detect_moves=not args.no_moves)
        _print_change_tree(changes, cmp.roots_for(changes, prof.paths, parent.paths),
                           args.depth, header=f"would snapshot (vs {parent.short_id} "
                           f"{parent.time[:19]}) -- nothing written:")
        return 0

    drivehealth.guard_writes(layout, force=args.force)
    with ops.Operation(layout, "snapshot", force=args.force,
                       force_reason="unclean-session" if args.force else None,
                       args={"profile": prof.name}, root=args.root,
                       message=args.message) as op:
        snap = syncmod.take_system_snapshot(
            store, layout, manifest, args.profile, root=args.root, extra_tags=op.tags()
        )
        op.record(result=snap, base=parent.id if parent else None)
        # This snapshot now represents this host's known-good state: record it
        # as the base used by sync's conflict detection.
        state = syncmod.HostState.load(store.repo_id())
        state.base_snapshot = snap
        state.save()
    drivehealth.mark_clean(layout)
    print(f"created system snapshot {snap[:8]}  (op {op.id}, profile {prof.name})")

    if args.no_diff:
        return 0
    if not parent:
        print("first snapshot of this profile from this host; nothing to compare against")
        return 0
    if parent.hostname != me:
        print(f"(comparing against {parent.short_id} from {parent.hostname}, "
              f"the snapshot this host last restored)")
    new = syncmod.snapshot_by_spec(store, snap)
    a = cmp.listing_from_snapshot(store, parent.id, under=parent.paths)
    b = cmp.listing_from_snapshot(store, new.id, under=new.paths)
    changes = cmp.diff_listings(a, b, detect_moves=not args.no_moves)
    _print_change_tree(changes, cmp.roots_for(changes, prof.paths, new.paths), args.depth,
                       header=f"changes since {parent.short_id} ({parent.time[:19]}):")
    if new.stats:
        print(f"restic: {new.stats} files, {_human(new.summary.get('data_added', 0))} added to repo")
    _hint_duplicates(changes)
    return 0


def _hint_duplicates(changes) -> None:
    dups = [c for c in changes if c.status == "duplicate"]
    if dups:
        print("\nnote: directories marked '=' are identical copies of a directory that")
        print("      also exists elsewhere -- typically the OLD location of a move that a")
        print("      restore brought back. Remove the stale one, or use 'restore --mirror'.")


def _human(n) -> str:
    return cmp._fmt_size(int(n or 0))


def cmd_list(args) -> int:
    layout, store = _layout_store(args)
    snaps = store.snapshots()
    if not snaps:
        print("no snapshots yet")
        return 0
    rows = ops.annotate(snaps, ops.read_journal(layout))
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.host:
        rows = [r for r in rows if r["snap"].hostname == args.host]
    if args.profile:
        rows = [r for r in rows if r["profile"] == args.profile]
    if args.op:
        rows = [r for r in rows if r["op"] and r["op"].startswith(args.op)]
    if args.forced:
        # forced results AND the safety snapshots taken right before them
        forced_ops = {r["op"] for r in rows if r["forced"] and r["op"]}
        rows = [r for r in rows if r["forced"] or (r["op"] in forced_ops)]
    if args.near:
        target = syncmod.snapshot_by_spec(store, args.near, [r["snap"] for r in rows])
        idx = next(i for i, r in enumerate(rows) if r["snap"].id == target.id)
        rows = rows[max(0, idx - args.near_n): idx + args.near_n + 1]
    if args.last:
        rows = rows[-args.last:]
    if not rows:
        print("no matching snapshots")
        return 0
    print(f"{'id':8}  {'time':19}  {'kind':7}  {'host':12} {'profile':10} {'op':6}  "
          f"{'flags':8} note")
    for r in rows:
        s: Snapshot = r["snap"]
        flags = "FORCED" if r["forced"] else ""
        note = ""
        if r["kind"] == "safety":
            note = f"before {r['pre'] or '?'}"
        elif r["kind"] == "commit":
            try:
                note = ws.read_commit_meta(store, s).get("message", "")
            except PortaWinuxError:
                pass
        elif r["via"]:
            note = f"via {r['via']}"
        if s.stats and r["kind"] != "commit":
            note += f"  [{s.stats}]"
        if r["message"]:
            note += f'  "{r["message"]}"'
        if r["undo"]:
            note += f"  undo: restore {r['undo'][:8]}"
        print(f"{s.short_id:8}  {s.time[:19]}  {r['kind']:7}  {s.hostname[:12]:12} "
              f"{(r['profile'] or '-')[:10]:10} {(r['op'] or '-'):6}  {flags:8} {note}".rstrip())
    return 0


def cmd_log(args) -> int:
    layout, _ = _layout_store(args)
    recs = ops.read_journal(layout)
    if args.forced:
        recs = [r for r in recs if r.get("forced")]
    if args.op:
        recs = [r for r in recs if r["op"].startswith(args.op)]
    if args.cmd:
        recs = [r for r in recs if r["cmd"] == args.cmd]
    if args.last:
        recs = recs[-args.last:]
    if not recs:
        print("no operations recorded" + (" (matching)" if (args.forced or args.op or args.cmd) else "")
              + " -- the journal starts with the first write after upgrading to 0.4")
        return 0
    for r in recs:
        print(ops.format_journal_line(r))
        snaps = r.get("snapshots") or {}
        if r.get("forced") and snaps.get("safety"):
            print(f"        undo: porta-winux restore {str(snaps['safety'])[:8]}"
                  + ("" if not snaps.get("result") else
                     f"   (state right before op {r['op']} overwrote things)"))
        for n in r.get("notes") or []:
            print(f"        {n}")
    return 0


def cmd_ls(args) -> int:
    layout, store = _layout_store(args)
    snap = _resolve_snapshot(store, args.snapshot)
    for node in store.ls(snap):
        if node.get("type") == "file":
            print(node["path"])
    return 0


def cmd_checkout(args) -> int:
    layout, store = _layout_store(args)
    from .hooks import run_hooks

    snap = _resolve_snapshot(store, args.snapshot)
    run_hooks(layout, "pre-checkout", {"snapshot": snap})
    files = ws.checkout(store, layout, snap, args.paths)
    for f in files:
        print(f"  {f}")
    print(f"checked out {len(files)} file(s) into {layout.workspace_root}")
    print("edit them there, then: porta-winux commit -m 'what changed'")
    return 0


def cmd_status(args) -> int:
    layout, _ = _layout_store(args)
    files = ws.status(layout)
    if not files:
        print("workspace empty")
    for f in files:
        print(f"  {f}")
    return 0


def cmd_commit(args) -> int:
    layout, store = _layout_store(args)
    drivehealth.guard_writes(layout, force=getattr(args, 'force', False))
    latest_system = [s for s in store.snapshots(tags=["system"])]
    base = latest_system[-1].id if latest_system else None
    with ops.Operation(layout, "commit", force=args.force,
                       force_reason="unclean-session" if args.force else None,
                       message=args.message) as op:
        snap = ws.commit(store, layout, args.message, base, extra_tags=op.tags())
        op.record(result=snap, base=base)
    drivehealth.mark_clean(layout)
    print(f"committed as {snap[:8]}: {args.message}")
    return 0


def cmd_sync(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()

    # Always compute the plan first and show it.
    plan = syncmod.sync(store, layout, manifest, root=args.root, force=args.force, dry_run=True)
    if not (plan.written or plan.conflicts or plan.skipped_same):
        print("nothing to sync")
        return 0
    print(f"sync plan (target root: {args.root}):")
    syncmod.print_sync_result(plan)
    if args.dry_run:
        return 1 if plan.conflicts and not args.force else 0
    if plan.written:
        interact.confirm(
            f"Write {len(plan.written)} file(s) to {args.root} "
            "(a safety snapshot is taken first)?",
            assume_yes=args.yes,
        )

    drivehealth.guard_writes(layout, force=args.force)
    with ops.Operation(layout, "sync", force=args.force,
                       force_reason="overwrite-conflicts" if args.force else None,
                       args={"commits": [c[:8] for c in
                             (c.id for c in syncmod.pending_commits(
                                 store, syncmod.HostState.load(store.repo_id())))]},
                       root=args.root) as op:
        result = syncmod.sync(
            store, layout, manifest, root=args.root, force=args.force, dry_run=False,
            safety_tags=op.safety_tags(),
        )
        op.record(safety=result.safety_snapshot)
        syncmod.print_sync_result(result)
        if result.conflicts and args.force:
            op.note(f"overwrote {len(result.conflicts)} conflicting local file(s)")
        if result.applied_commits and not args.no_snapshot:
            snap = syncmod.take_system_snapshot(store, layout, manifest, None, root=args.root,
                                                extra_tags=op.tags())
            state = syncmod.HostState.load(store.repo_id())
            state.base_snapshot = snap
            state.save()
            op.record(result=snap)
            print(f"post-sync system snapshot {snap[:8]} recorded as new base")
    drivehealth.mark_clean(layout)
    return 1 if result.conflicts and not args.force else 0


def cmd_restore(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    # `restore /some/path` (no snapshot) -> auto-select the snapshot, restore that path.
    spec, paths = args.snapshot, list(args.paths)
    if spec and spec.startswith("/"):
        paths.insert(0, spec)
        spec = None
    snap = syncmod.select_restore_snapshot(store, manifest, args.profile, spec,
                                           host=args.host, any_host=args.any_host)
    me = hostname()
    scope = ", ".join(paths) if paths else "every path in the snapshot"
    prof_name = ops.profile_of(snap)
    print(f"restore plan: {snap.short_id}  {snap.time[:19]}  from host {snap.hostname}"
          + (" (this machine)" if snap.hostname == me else "")
          + (f"  profile {prof_name}" if prof_name else "")
          + (f"  op {ops.op_of(snap)}" if ops.op_of(snap) else ""))
    print(f"  onto {args.root}: {scope}")
    if args.mirror:
        print("  MIRROR: files under those paths that are NOT in the snapshot will be DELETED"
              " (profile excludes are protected)")
    if not args.no_safety:
        print("  a safety snapshot of the current state of those paths is taken first")

    # Preview the effect: live vs snapshot, restricted to the restored paths.
    # With --mirror the 'removed' entries are exactly the delete list
    # (same listing logic as sync.stale_paths: profile excludes protected).
    if not args.no_preview:
        roots = paths or snap.paths
        a = cmp.listing_from_live(roots, syncmod.profile_excludes(manifest, snap), root=args.root)
        b = cmp.listing_from_snapshot(store, snap.id, under=roots)
        changes = cmp.diff_listings(a, b, detect_moves=not args.no_moves)
        if not args.mirror:
            # without --mirror, files only present locally are left alone;
            # a "moved" here means the old location would stay as a copy
            kept = [c for c in changes if c.status in ("removed", "duplicate", "moved")]
            changes = [c for c in changes if c.status not in ("removed", "duplicate", "moved")]
            if kept:
                print(f"  note: {len(kept)} local path(s) not in the snapshot are left in place "
                      f"(--mirror would delete them):")
                for c in kept[:10]:
                    print(f"        {c.path}" + (f"  (moved to {c.other})" if c.status == "moved" else ""))
                if len(kept) > 10:
                    print(f"        … {len(kept) - 10} more")
        _print_change_tree(changes, cmp.roots_for(changes, _profile_roots(manifest, prof_name), roots),
                           args.depth, header="  effect on this system (live -> snapshot):")
        if not changes and not args.dry_run:
            print("  this system already matches the snapshot for those paths")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    interact.confirm("Proceed with restore?", assume_yes=args.yes)
    drivehealth.guard_writes(layout)
    with ops.Operation(layout, "restore", args={"snapshot": snap.short_id, "from_host": snap.hostname,
                                                "paths": paths, "mirror": args.mirror},
                       root=args.root, message=args.message) as op:
        safety, deleted = syncmod.restore(store, layout, manifest, snap, paths or None,
                                          root=args.root, mirror=args.mirror,
                                          safety=not args.no_safety, safety_tags=op.safety_tags())
        op.record(safety=safety, result=snap.id)
        if deleted:
            op.note(f"mirror deleted {len(deleted)} path(s)")
    drivehealth.mark_clean(layout)
    if safety:
        print(f"safety snapshot: {safety[:8]}   (undo: porta-winux restore {safety[:8]})")
    if deleted:
        print(f"mirror: deleted {len(deleted)} stale path(s) not in the snapshot")
    print(f"restored {snap.short_id} ({snap.time[:19]}, {snap.hostname}) onto {args.root}"
          + ("" if paths else " -- this host's base is now that snapshot"))
    return 0


def cmd_compare(args) -> int:
    layout, store = _layout_store(args)
    manifest = layout.load_manifest()
    specs = list(args.snapshots)
    all_snaps = store.snapshots()

    def resolve(spec: str) -> Snapshot:
        if spec == "latest":
            return syncmod.select_restore_snapshot(store, manifest, args.profile, "latest")
        return syncmod.snapshot_by_spec(store, spec, all_snaps)

    if len(specs) >= 3:
        snaps = [resolve(x) for x in specs]
        listings = [cmp.listing_from_snapshot(store, s.id, under=s.paths) for s in snaps]
        prof0 = ops.profile_of(snaps[0]) or (args.profile or manifest.default_profile)
        all_changes = [c for step in cmp.history(listings, detect_moves=not args.no_moves)[0] for c in step]
        roots = cmp.roots_for(all_changes, _profile_roots(manifest, prof0), snaps[0].paths)
        steps, tree = cmp.history(listings, detect_moves=not args.no_moves, roots=roots)
        labels = [f"{s.short_id}@{s.hostname[:8]}" for s in snaps]
        print(cmp.render_history(tree, labels, depth=args.depth, show_all=args.all))
        return 0

    if not specs:
        # latest snapshot of the profile (this host preferred) vs the live system
        prof = manifest.profile(args.profile)
        cands = [s for s in all_snaps if s.is_system and f"profile:{prof.name}" in s.tags]
        mine = [s for s in cands if s.hostname == hostname()]
        if not cands:
            raise PortaWinuxError(f"no system snapshots for profile '{prof.name}'")
        snap_a, snap_b = (mine or cands)[-1], None
    elif len(specs) == 1:
        snap_a, snap_b = resolve(specs[0]), None
    else:
        snap_a, snap_b = resolve(specs[0]), resolve(specs[1])

    prof_name = ops.profile_of(snap_a) or (args.profile or manifest.default_profile)
    prof_paths = _profile_roots(manifest, prof_name)
    excludes = manifest.profiles[prof_name].excludes if prof_name in manifest.profiles else []
    a = cmp.listing_from_snapshot(store, snap_a.id, under=snap_a.paths)
    label_a = f"{snap_a.short_id} ({snap_a.time[:19]}, {snap_a.hostname})"
    if snap_b is None:
        b = cmp.listing_from_live(snap_a.paths, excludes, root=args.root)
        label_b = f"live system ({args.root})"
        read_b = lambda p: cmp.local_path(args.root, p).read_bytes()  # noqa: E731
    else:
        b = cmp.listing_from_snapshot(store, snap_b.id, under=snap_b.paths)
        label_b = f"{snap_b.short_id} ({snap_b.time[:19]}, {snap_b.hostname})"
        read_b = lambda p: store.dump(snap_b.id, p)  # noqa: E731
    read_a = lambda p: store.dump(snap_a.id, p)  # noqa: E731
    changes = cmp.diff_listings(a, b, detect_moves=not args.no_moves)
    if args.status:
        changes = [c for c in changes if c.status == args.status]
    roots = cmp.roots_for(changes, prof_paths, snap_a.paths)

    if args.interactive:
        try:
            from .compare_tui import run_compare_tui
        except ImportError:
            raise PortaWinuxError("interactive compare needs Textual: pip install 'porta-winux[tui]'")
        return run_compare_tui(cmp.build_tree(changes, roots), f"{snap_a.short_id} -> "
                               + (snap_b.short_id if snap_b else "live"),
                               label_a, label_b, read_a, read_b)
    if args.flat:
        for line in cmp.flat_lines(changes):
            print(line)
    else:
        _print_change_tree(changes, roots, args.depth, expand=args.expand, show_all=args.all,
                           header=f"{label_a}\n  -> {label_b}")
    tree = cmp.build_tree(changes, roots)
    total = sum(n.counts.total() for n in tree.children.values())
    if changes:
        print(f"\n{total} change(s): {cmp.summarize(sum((n.counts for n in tree.children.values()),
                                                       start=__import__('collections').Counter()))}"
              f"   ({cmp.GLYPH['added']} added  {cmp.GLYPH['removed']} removed  {cmp.GLYPH['modified']}"
              f" modified  {cmp.GLYPH['moved']} moved  {cmp.GLYPH['duplicate']} duplicate  T type)")
    _hint_duplicates(changes)
    if args.confirm_moves and snap_b is not None:
        for c in changes:
            if c.status == "moved":
                ok = cmp.confirm_move(store, snap_a.id, snap_b.id, c.path, c.other)
                print(f"  {c.path} -> {c.other}: {'CONFIRMED identical content' if ok else 'content differs (edited during move?)'}")
    return 0


def cmd_diff(args) -> int:
    layout, store = _layout_store(args)
    for e in store.diff(args.a, args.b):
        print(f"{e.action}  {e.path}")
    return 0


def cmd_verify(args) -> int:
    layout, store = _layout_store(args)
    fstype, target, _ = drivehealth.fs_info(layout.root)
    pinned = drivehealth.pinned_fstype(layout)
    state = drivehealth.session_state(layout)
    print(f"drive: {target or layout.root}  fs={fstype}"
          + (f" (pinned: {pinned})" if pinned else "  (fs not pinned in manifest)"))
    if state == "dirty":
        print("previous session ended UNCLEANLY — verifying before clearing the flag"
              + ("" if args.deep else " (use --deep after a raw unplug)"))

    print("tier 1: pack files vs their content hashes…")
    bad = drivehealth.verify_packs(layout)
    if bad:
        drivehealth.mark_dirty(layout)  # block all writes until a verify passes
        print(f"CORRUPTION: {len(bad)} pack file(s) do not hash to their names:",
              file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        print("do NOT run 'restic repair' reflexively — read STORAGE.md first.",
              file=sys.stderr)
        return 1

    print("tier 2: restic check" + (" --read-data (full)…" if args.deep else "…"))
    try:
        store.check(read_data=args.deep)
    except Exception:
        drivehealth.mark_dirty(layout)  # block all writes until a verify passes
        raise

    drivehealth.mark_clean(layout)
    if fstype == "btrfs" and args.deep:
        print("btrfs tip: 'sudo btrfs scrub start -B " + (target or "<mount>") + "'"
              " additionally verifies filesystem checksums")
    print("repository integrity OK" + (" (deep)" if args.deep else ""))
    return 0


def cmd_prune(args) -> int:
    layout, store = _layout_store(args)
    print(f"prune plan: PERMANENTLY delete system/safety snapshots beyond the "
          f"newest {args.keep_last} (commits are never pruned)")
    interact.confirm("Proceed with prune?", assume_yes=args.yes)
    drivehealth.guard_writes(layout)
    store.forget(keep_last=args.keep_last)
    drivehealth.mark_clean(layout)
    print(f"pruned; kept last {args.keep_last} system snapshots (all commits retained)")
    return 0


def cmd_init_format(args) -> int:
    notes = drivehealth.format_drive_checks(args.device)
    subprocess_out = __import__("subprocess").run(
        ["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,MODEL", args.device],
        capture_output=True, text=True).stdout
    print(subprocess_out)
    for n in notes:
        print(n)
    win_line = "  3  PW_WIN     rest    NTFS   reserved for a future Windows repo\n" \
        if not args.no_win else ""
    print(
        f"This will DESTROY ALL DATA on {args.device} and create:\n"
        f"  1  PW_SHARED  {args.shared}    exFAT  cross-platform human files\n"
        f"  2  PW_LINUX   {args.linux if not args.no_win else 'rest'}   "
        f"{args.fstype}  the repo — invisible to Windows (GPT type 8300)\n"
        + win_line
    )
    if args.dry_run:
        drivehealth.format_drive(args.device, args.shared, args.linux, args.fstype,
                                 args.dup_data, not args.no_win, dry_run=True)
        return 0
    # Destructive formatting is interactive-only: --yes is deliberately NOT
    # honored here, and the confirmation is typing the device path itself.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PortaWinuxError("init format is interactive-only (no --yes bypass)")
    typed = input(f"Type the device path ({args.device}) to confirm: ").strip()
    if typed != args.device:
        raise PortaWinuxError("confirmation did not match; nothing was changed")
    drivehealth.format_drive(args.device, args.shared, args.linux, args.fstype,
                             args.dup_data, not args.no_win)
    print("\ndone. Next steps:")
    print("  1. unplug and replug the drive (KDE will mount PW_SHARED and PW_LINUX)")
    print("  2. porta-winux init drive        # pick the PW_LINUX mount from the menu")
    print(f"     (init pins fstype={args.fstype}; writes are refused on anything else)")
    print("  optional: stop auto-mounting the repo partition —")
    print("     sudo cp setup/99-portawinux.rules /etc/udev/rules.d/ && sudo udevadm control --reload")
    return 0


def cmd_init_drive(args) -> int:
    drivesetup.init_drive(args.path, args.password, args.yes)
    return 0


def cmd_init_detect(args) -> int:
    return drivesetup.detect(args.drive, with_repo=not args.no_repo)


def cmd_init_cleanup(args) -> int:
    layout = find_drive(args.drive)
    return drivesetup.cleanup(layout, args.yes, all_hosts_state=args.all_state)


def cmd_init_uninstall(args) -> int:
    layout = None
    if not args.host_only:
        layout = find_drive(args.drive)
    return drivesetup.uninstall(layout, drive=not args.host_only, host=not args.drive_only,
                                purge=args.purge)


def cmd_eject(args) -> int:
    layout, _store = _layout_store(args)
    drivehealth.eject(layout, force=args.force)
    return 0


def cmd_mount(args) -> int:
    layout, store = _layout_store(args)
    print(f"mounting repository at {args.target} (Ctrl-C to unmount)")
    store.mount(Path(args.target))
    return 0


def cmd_browse(args) -> int:
    layout, store = _layout_store(args)
    try:
        from .browse import run_browser
    except ImportError:
        print(
            "the browser needs Textual: pip install 'porta-winux[tui]'\n"
            "meanwhile, try:  porta-winux ls latest | fzf  or  porta-winux mount /tmp/snap",
            file=sys.stderr,
        )
        return 1
    snap = _resolve_snapshot(store, args.snapshot)
    return run_browser(store, layout, snap)




def cmd_pkg_scan(args) -> int:
    layout = find_drive(args.drive)
    current = pkgmod.PackageState.load(layout.root / pkgmod.PACKAGES_NAME)
    scanned, notes = pkgmod.scan_system()
    for n in notes:
        print(f"note: {n}")

    if args.capture_baseline:
        path = pkgmod.write_baseline(
            layout, scanned.dnf_packages,
            f"host scan on Fedora {scanned.fedora_release or '?'}",
        )
        print(f"captured baseline of {len(scanned.dnf_packages)} packages -> {path}")
        print("future scans (on any machine) subtract these from the draft")

    baseline = pkgmod.load_baseline(layout)
    raw_count = len(scanned.dnf_packages)
    if baseline:
        scanned.dnf_packages = [p for p in scanned.dnf_packages if p not in baseline]
        print(f"baseline subtraction: {raw_count} -> {len(scanned.dnf_packages)} dnf candidates")

    draft = layout.root / pkgmod.PACKAGES_DRAFT
    draft.write_text(pkgmod.render_packages_draft(current, scanned))
    new_dnf = len(set(scanned.dnf_packages) - set(current.dnf_packages))
    new_fp = len({a["id"] for a in scanned.flatpak_apps} - {a["id"] for a in current.flatpak_apps})
    print(f"scanned this host: {len(scanned.dnf_packages)} user-installed dnf packages "
          f"({new_dnf} new vs list), {len(scanned.flatpak_apps)} flatpak apps ({new_fp} new), "
          f"{len(scanned.dnf_repos)} non-default repos")
    print(f"draft written (nothing else touched): {draft}")
    print("review/edit it — delete anything you don't want synced — then: porta-winux pkg adopt")
    return 0


def cmd_pkg_scan_configs(args) -> int:
    layout = find_drive(args.drive)
    print("scanning rpm database for altered /etc config files (may take a minute)…")
    try:
        etc = pkgmod.scan_modified_etc()
    except pkgmod.ScanUnavailable as e:
        raise PortaWinuxError(f"config scan needs rpm: {e}")
    if os.geteuid() != 0:
        print("note: running unprivileged; some root-only files may be missed "
              "(re-run with sudo for a complete scan)")
    state = pkgmod.PackageState.load(layout.root / pkgmod.PACKAGES_NAME)
    hints = pkgmod.user_config_hints(state)
    draft = layout.root / pkgmod.CONFIGS_DRAFT
    draft.write_text(pkgmod.render_configs_draft(etc, hints))
    sens = sum(1 for p in etc if pkgmod.is_sensitive(p))
    print(f"found {len(etc)} altered /etc config file(s) "
          f"({sens} flagged SENSITIVE and left commented out), "
          f"{len(hints)} suggested user config path(s) (all commented out)")
    print(f"draft written: {draft}")
    print("review/edit, then: porta-winux pkg adopt")
    return 0


def cmd_pkg_adopt(args) -> int:
    layout = find_drive(args.drive)
    plan = pkgmod.adopt_plan(layout)
    if not plan:
        print("no drafts on the drive; run 'pkg scan' / 'pkg scan-configs' first")
        return 1
    print("adopt plan (changes the lists on the drive only — installs nothing, removes nothing):")
    for line in plan:
        print("  " + line)
    interact.confirm("Adopt draft(s) into the drive's designation files?", assume_yes=args.yes)
    for name in pkgmod.adopt_execute(layout):
        print(f"adopted {name}")
    if (layout.root / pkgmod.CONFIGS_NAME).exists():
        print("configs are now the 'system-configs' profile; snapshot them with:")
        print("  sudo porta-winux snapshot -p system-configs")
    return 0


def cmd_pkg_diff(args) -> int:
    layout = find_drive(args.drive)
    plan = pkgmod.build_apply_plan(layout)
    pkgmod.print_plan(plan)
    return 0


def cmd_pkg_apply(args) -> int:
    layout = find_drive(args.drive)
    plan = pkgmod.build_apply_plan(layout)
    pkgmod.print_plan(plan)
    if not plan.has_work:
        print("nothing to install — this machine already has everything in the list")
        return 0
    if args.dry_run:
        print("dry run — the exact commands a real apply would run:")
        pkgmod.apply_dnf(plan.dnf_to_install, dry_run=True)
        pkgmod.apply_flatpak(plan.remotes_to_add, plan.flatpak_to_install, dry_run=True)
        print("(nothing was executed)")
        return 0
    failed: list[str] = []
    if plan.dnf_to_install:
        interact.confirm(
            f"Install {len(plan.dnf_to_install)} dnf package(s) via "
            f"'{'sudo ' if os.geteuid() else ''}dnf install' (install only, never removes)?",
            assume_yes=args.yes,
        )
        failed += pkgmod.apply_dnf(plan.dnf_to_install, dry_run=False)
    if plan.remotes_to_add or plan.flatpak_to_install:
        interact.confirm(
            f"Add {len(plan.remotes_to_add)} flatpak remote(s) and install "
            f"{len(plan.flatpak_to_install)} flatpak app(s)?",
            assume_yes=args.yes,
        )
        failed += pkgmod.apply_flatpak(plan.remotes_to_add, plan.flatpak_to_install, dry_run=False)
    if plan.repos_missing:
        print("reminder: these repos are still missing here (set them up, then re-run "
              "'pkg apply'): " + ", ".join(plan.repos_missing))
    if failed:
        print(f"{len(failed)} item(s) failed to install: {', '.join(failed)}")
        print("fix names/repos in packages.toml (or set up the missing repos) and re-run 'pkg apply'")
        return 1
    print("apply complete")
    return 0


# -- plumbing ----------------------------------------------------------------


def _resolve_snapshot(store, spec: str) -> str:
    if spec != "latest":
        return spec
    snaps = store.snapshots(tags=["system"])
    if not snaps:
        raise PortaWinuxError("no system snapshots yet; run 'porta-winux snapshot' first")
    return snaps[-1].id


def _deprecated(new: str):
    def wrap(func):
        def inner(args):
            print(f"note: this spelling is deprecated; use 'porta-winux {new}'", file=sys.stderr)
            return func(args)
        return inner
    return wrap


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="porta-winux",
        description="snapshot/checkout/commit/sync/restore over a restic repo on an external drive",
        epilog="run with no command to see which porta-winux drives are plugged in",
    )
    p.add_argument("--drive", help=f"drive root (dir containing {MANIFEST_NAME})")
    p.add_argument(
        "--root",
        default="/",
        help="target system root (default /; point at a sandbox dir for testing)",
    )
    p.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip confirmation prompts (for scripts, kickstart, CI)",
    )
    p.add_argument("--eject", action="store_true",
                   help="after the command succeeds, eject the drive (sync, unmount, power off)")
    p.add_argument("--version", action="version", version=f"porta-winux {__version__}")
    sub = p.add_subparsers(dest="command", required=False, metavar="COMMAND")

    # -- init group ----------------------------------------------------------
    s = sub.add_parser("init", help="set up / inspect / take apart a porta-winux drive")
    isub = s.add_subparsers(dest="init_cmd", required=True)

    f = isub.add_parser("format", help="DESTRUCTIVE: partition a disk (exFAT shared / btrfs repo / NTFS reserved)")
    f.add_argument("device", help="whole-disk device, e.g. /dev/sda")
    f.add_argument("--shared", default="16G", help="PW_SHARED size (default 16G)")
    f.add_argument("--linux", default="200G", help="PW_LINUX size (default 200G; ignored with --no-win)")
    f.add_argument("--fstype", choices=["btrfs", "ext4"], default="btrfs")
    f.add_argument("--dup-data", action="store_true",
                   help="btrfs: store data twice (halves capacity, enables self-repair)")
    f.add_argument("--no-win", action="store_true", help="skip the reserved Windows partition")
    f.add_argument("-n", "--dry-run", action="store_true", help="print the exact commands only")
    f.set_defaults(func=cmd_init_format)

    d = isub.add_parser("drive", help="furnish a mounted partition as a porta-winux drive (idempotent)")
    d.add_argument("path", nargs="?", help="drive root (interactive picker if omitted)")
    d.add_argument("--password", help="repo password (default: generate and store on drive)")
    d.set_defaults(func=cmd_init_drive)

    d = isub.add_parser("detect", help="read-only: which porta-winux drives are plugged in, and their state")
    d.add_argument("--no-repo", action="store_true", help="skip opening the repo (faster, no password needed)")
    d.set_defaults(func=cmd_init_detect)

    d = isub.add_parser("cleanup", help="tidy: this host's state for the drive, stale locks, unadopted drafts")
    d.add_argument("--all-state", action="store_true", help="remove host state for EVERY repo, not just this drive")
    d.set_defaults(func=cmd_init_cleanup)

    d = isub.add_parser("uninstall", help="DESTRUCTIVE: remove porta-winux from the drive and this host")
    d.add_argument("--host-only", action="store_true", help="only remove this host's state")
    d.add_argument("--drive-only", action="store_true", help="only remove the drive's porta-winux files")
    d.add_argument("--purge", action="store_true", help="also remove packages/configs/baseline .toml")
    d.set_defaults(func=cmd_init_uninstall)

    # -- daily commands --------------------------------------------------------
    s = sub.add_parser("snapshot", help="take a system snapshot of a profile and show what changed")
    s.add_argument("-p", "--profile")
    s.add_argument("-m", "--message", help="free-text note recorded in the journal")
    s.add_argument("--force", action="store_true",
                   help="write even after an unclean session (verify first instead!)")
    s.add_argument("-n", "--dry-run", action="store_true",
                   help="show what a snapshot would capture (live vs last snapshot); write nothing")
    s.add_argument("--depth", type=int, default=2, help="tree depth to show (default 2)")
    s.add_argument("--no-diff", action="store_true", help="skip the change summary")
    s.add_argument("--no-moves", action="store_true", help="skip move/duplicate detection")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("list", help="list snapshots with the operation that made them")
    s.add_argument("--kind", choices=["system", "commit", "safety", "other"])
    s.add_argument("--host")
    s.add_argument("-p", "--profile")
    s.add_argument("--op", help="only snapshots from this operation id (prefix ok)")
    s.add_argument("--forced", action="store_true",
                   help="only forced operations and the safety snapshots taken right before them")
    s.add_argument("--near", metavar="SNAPSHOT", help="show snapshots around this one")
    s.add_argument("--near-n", type=int, default=3, help="how many on each side of --near (default 3)")
    s.add_argument("-n", "--last", type=int, help="only the newest N")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("log", help="operation journal: every write command, its args, and the snapshots it made")
    s.add_argument("--forced", action="store_true", help="only --force operations (each with its undo snapshot)")
    s.add_argument("--op", help="one operation id (prefix ok)")
    s.add_argument("--cmd", help="only this command (snapshot, sync, restore, commit)")
    s.add_argument("-n", "--last", type=int)
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("ls", help="list files inside a snapshot")
    s.add_argument("snapshot", nargs="?", default="latest")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("checkout", help="extract files from a snapshot into the drive workspace")
    s.add_argument("paths", nargs="+", help="absolute paths (files or dirs) to check out")
    s.add_argument("-s", "--snapshot", default="latest")
    s.set_defaults(func=cmd_checkout)

    s = sub.add_parser("status", help="show files currently in the workspace")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("commit", help="commit workspace edits as a new snapshot")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--force", action="store_true",
                   help="write even after an unclean session (verify first instead!)")
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("sync", help="apply pending commits to this system")
    s.add_argument("--force", action="store_true", help="overwrite conflicting local edits")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("--no-snapshot", action="store_true", help="skip the post-sync base snapshot")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser(
        "restore",
        help="lay a snapshot onto this system: whole (default: latest from ANOTHER machine) or given paths",
        description="restore [SNAPSHOT] [PATH ...]\n"
                    "  restore                    latest snapshot of the profile from another machine\n"
                    "  restore latest             latest from any machine (including this one)\n"
                    "  restore a1b2c3             that snapshot, entirely\n"
                    "  restore a1b2c3 /home/u/x   only that path from it\n"
                    "  restore /home/u/x          only that path, snapshot auto-selected\n"
                    "A safety snapshot of the current state is taken first; undo is 'restore <safety-id>'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("snapshot", nargs="?", help="snapshot id/prefix, 'latest', or omit for other-machine latest")
    s.add_argument("paths", nargs="*", help="limit to these absolute paths")
    s.add_argument("-p", "--profile")
    s.add_argument("--host", help="take the latest snapshot from this host")
    s.add_argument("--any-host", action="store_true", help="allow this machine's own snapshots when auto-selecting")
    s.add_argument("--mirror", action="store_true",
                   help="also DELETE local files under the restored paths that are not in the snapshot "
                        "(fixes stale copies left by moves; profile excludes are protected)")
    s.add_argument("--no-safety", action="store_true", help="skip the safety snapshot")
    s.add_argument("--no-preview", action="store_true", help="skip computing the change preview")
    s.add_argument("--no-moves", action="store_true", help="skip move/duplicate detection in the preview")
    s.add_argument("--depth", type=int, default=2, help="preview tree depth (default 2)")
    s.add_argument("-n", "--dry-run", action="store_true", help="show the plan and preview only")
    s.add_argument("-m", "--message", help="free-text note recorded in the journal")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser(
        "compare",
        help="what changed: latest snapshot vs live system, two snapshots, or a chain of them",
        description="compare                  latest snapshot of the profile (this host) vs the live system\n"
                    "compare A                snapshot A vs the live system\n"
                    "compare A B              snapshot A vs snapshot B\n"
                    "compare A B C ...        per-step history across a chain of snapshots\n"
                    "Shown as a tree rooted at the profile paths, collapsed to --depth; "
                    "--expand PATH opens one subtree, --all opens everything, -i is interactive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("snapshots", nargs="*", help="snapshot ids/prefixes or 'latest'")
    s.add_argument("-p", "--profile")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--expand", action="append", metavar="PATH", help="fully expand this subtree (repeatable)")
    s.add_argument("--all", action="store_true", help="expand everything")
    s.add_argument("--flat", action="store_true", help="one line per change instead of a tree")
    s.add_argument("--status", choices=["added", "removed", "modified", "moved", "duplicate", "type"],
                   help="only this kind of change")
    s.add_argument("--no-moves", action="store_true", help="skip move/duplicate detection")
    s.add_argument("--confirm-moves", action="store_true",
                   help="verify detected moves with restic content hashes (snapshot-vs-snapshot only)")
    s.add_argument("-i", "--interactive", action="store_true", help="interactive tree (Textual)")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("diff", help="raw restic diff of two snapshots (one line per path; for scripts)")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("verify", help="check repo integrity (pack hashes + restic check)")
    s.add_argument("--deep", action="store_true",
                   help="full restic check --read-data (run after any unsafe unplug)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("eject", help="sync, mark clean, unmount, power off: SAFE TO UNPLUG")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_eject)

    s = sub.add_parser("prune", help="thin old system/safety snapshots (keeps all commits)")
    s.add_argument("--keep-last", type=int, default=10)
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("mount", help="FUSE-mount the repo for ad-hoc browsing/grep")
    s.add_argument("target")
    s.set_defaults(func=cmd_mount)

    s = sub.add_parser(
        "pkg",
        help="user-designated programs & configs: scan, adopt, diff, apply (install-only)",
    )
    pkg_sub = s.add_subparsers(dest="pkg_cmd", required=True)
    ps = pkg_sub.add_parser("scan", help="scan this host, write editable packages draft")
    ps.add_argument(
        "--capture-baseline",
        action="store_true",
        help="ALSO record this machine's package set as the fresh-install baseline "
             "(run on a machine you haven't installed anything on yet)",
    )
    ps.set_defaults(func=cmd_pkg_scan)
    ps = pkg_sub.add_parser("scan-configs", help="find altered /etc configs + user config suggestions")
    ps.set_defaults(func=cmd_pkg_scan_configs)
    ps = pkg_sub.add_parser("adopt", help="promote edited draft(s) to the drive's designation files")
    ps.set_defaults(func=cmd_pkg_adopt)
    ps = pkg_sub.add_parser("diff", help="desired list vs this machine (report only)")
    ps.set_defaults(func=cmd_pkg_diff)
    ps = pkg_sub.add_parser("apply", help="install what's missing here (staged, confirmed, NEVER removes)")
    ps.add_argument("-n", "--dry-run", action="store_true")
    ps.set_defaults(func=cmd_pkg_apply)

    s = sub.add_parser("browse", help="interactive fuzzy search / preview / edit (Textual)")
    s.add_argument("-s", "--snapshot", default="latest")
    s.set_defaults(func=cmd_browse)

    # -- deprecated spellings (hidden; still work) -------------------------------
    s = sub.add_parser("init-drive")
    s.add_argument("path", nargs="?")
    s.add_argument("--password")
    s.set_defaults(func=_deprecated("init drive")(cmd_init_drive))

    s = sub.add_parser("format-drive")
    for a, kw in (("device", {}), ("--shared", {"default": "16G"}), ("--linux", {"default": "200G"}),
                  ("--fstype", {"choices": ["btrfs", "ext4"], "default": "btrfs"}),
                  ("--dup-data", {"action": "store_true"}), ("--no-win", {"action": "store_true"}),
                  ("-n", {"action": "store_true", "dest": "dry_run"})):
        s.add_argument(a, **kw)
    s.set_defaults(func=_deprecated("init format")(cmd_init_format))

    s = sub.add_parser("revert")
    s.add_argument("snapshot")
    s.add_argument("paths", nargs="*")
    s.set_defaults(func=_deprecated("restore <snapshot> [paths]")(cmd_restore),
                   profile=None, host=None, any_host=True, mirror=False, no_safety=False,
                   no_preview=False, no_moves=False, depth=2, dry_run=False, message=None)

    s = sub.add_parser("restore-full")
    s.add_argument("-p", "--profile")
    s.add_argument("-s", "--snapshot")
    s.set_defaults(func=_deprecated("restore [snapshot]")(cmd_restore),
                   paths=[], host=None, any_host=True, mirror=False, no_safety=False,
                   no_preview=False, no_moves=False, depth=2, dry_run=False, message=None)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.command:
            # "soft launch": no command = a read-only look at what's plugged in
            rc = drivesetup.detect(args.drive, with_repo=True)
            print("\nrun 'porta-winux --help' for the command list")
            return rc
        rc = args.func(args)
        if rc == 0 and args.eject:
            drivehealth.eject(find_drive(args.drive))
        return rc
    except PortaWinuxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # Downstream pipe (grep -q, head, …) closed early: normal, not an error.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
