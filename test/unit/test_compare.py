"""Unit tests for the compare engine (pure logic, no restic)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from porta_winux import compare as cmp  # noqa: E402

T = 1_700_000_000.0


def L(spec):
    """{path: (type, size, mtime)} -> Listing"""
    return {p: cmp.Entry(p, ty, sz, mt) for p, (ty, sz, mt) in spec.items()}


def statuses(changes):
    return {(c.path, c.status, c.other) for c in changes}


def test_added_removed_modified():
    a = L({"/h": ("dir", 0, T), "/h/a": ("file", 1, T), "/h/b": ("file", 2, T)})
    b = L({"/h": ("dir", 0, T), "/h/a": ("file", 1, T + 10), "/h/c": ("file", 3, T)})
    assert statuses(cmp.diff_listings(a, b)) == {
        ("/h/a", "modified", ""), ("/h/b", "removed", ""), ("/h/c", "added", "")}


def test_mtime_tolerance_and_dirs_are_not_modified():
    a = L({"/h": ("dir", 0, T), "/h/a": ("file", 1, T)})
    b = L({"/h": ("dir", 0, T + 500), "/h/a": ("file", 1, T + 1.5)})
    assert cmp.diff_listings(a, b) == []


def test_file_move_by_signature():
    a = L({"/h": ("dir", 0, T), "/h/x.txt": ("file", 10, T)})
    b = L({"/h": ("dir", 0, T), "/h/sub": ("dir", 0, T), "/h/sub/x.txt": ("file", 10, T)})
    ch = cmp.diff_listings(a, b)
    assert ("/h/x.txt", "moved", "/h/sub/x.txt") in statuses(ch)
    assert not any(c.path == "/h/sub/x.txt" for c in ch)          # consumed by the move
    assert ("/h/sub", "added", "") in statuses(ch)                 # the new dir itself


def test_directory_move_collapses_to_one_change():
    a = L({"/h": ("dir", 0, T), "/h/old": ("dir", 0, T), "/h/old/a": ("file", 5, T),
           "/h/old/s": ("dir", 0, T), "/h/old/s/b": ("file", 6, T)})
    b = L({"/h": ("dir", 0, T), "/h/new": ("dir", 0, T), "/h/new/a": ("file", 5, T),
           "/h/new/s": ("dir", 0, T), "/h/new/s/b": ("file", 6, T)})
    ch = cmp.diff_listings(a, b)
    assert len(ch) == 1
    assert ch[0].status == "moved" and ch[0].path == "/h/old" and ch[0].other == "/h/new"
    assert ch[0].files == 2


def test_move_plus_edit_is_not_a_move():
    a = L({"/h": ("dir", 0, T), "/h/old": ("dir", 0, T), "/h/old/a": ("file", 5, T)})
    b = L({"/h": ("dir", 0, T), "/h/new": ("dir", 0, T), "/h/new/a": ("file", 7, T + 60)})
    st = statuses(cmp.diff_listings(a, b))
    assert ("/h/old/a", "removed", "") in st and ("/h/new/a", "added", "") in st


def test_ambiguous_signatures_are_not_paired():
    a = L({"/h": ("dir", 0, T), "/h/p": ("file", 5, T), "/h/q": ("file", 5, T)})   # same size/mtime...
    b = L({"/h": ("dir", 0, T), "/h/d": ("dir", 0, T), "/h/d/p": ("file", 5, T), "/h/d/q": ("file", 5, T)})
    # ...but different basenames, so each pairs uniquely -> both move (dir collapse needs a removed dir)
    st = statuses(cmp.diff_listings(a, b))
    assert ("/h/p", "moved", "/h/d/p") in st and ("/h/q", "moved", "/h/d/q") in st


def test_stale_copy_is_flagged_duplicate():
    """The README item-6 scenario: after a restore, the old location exists
    next to the new one with identical contents."""
    a = L({"/h": ("dir", 0, T), "/h/new": ("dir", 0, T), "/h/new/a": ("file", 5, T)})
    b = L({"/h": ("dir", 0, T), "/h/new": ("dir", 0, T), "/h/new/a": ("file", 5, T),
           "/h/old": ("dir", 0, T + 9), "/h/old/a": ("file", 5, T)})
    ch = cmp.diff_listings(a, b)
    assert len(ch) == 1 and ch[0].status == "duplicate"
    assert ch[0].path == "/h/old" and ch[0].other == "/h/new" and ch[0].files == 1


def test_excludes_restic_style():
    m = cmp.matches_exclude
    assert m("/home/u/.cache/x", ["**/.cache"])
    assert m("/home/u/.cache", ["**/.cache"])
    assert m("/etc/shadow-", ["/etc/shadow*"])
    assert not m("/home/etc/shadow", ["/etc/shadow*"])
    assert m("/a/b/node_modules/c/d", ["**/node_modules"])
    assert m("/home/u/.local/share/Trash/f", ["**/.local/share/Trash"])
    assert m("/x/big.iso", ["**/*.iso"])
    assert not m("/home/u/src/x.py", ["**/.cache", "**/*.iso"])


def test_live_listing_applies_excludes(tmp_path):
    (tmp_path / "home/u/.cache").mkdir(parents=True)
    (tmp_path / "home/u/.cache/junk").write_text("j")
    (tmp_path / "home/u/f.txt").write_text("f")
    (tmp_path / "home/u/link").symlink_to("f.txt")
    lst = cmp.listing_from_live([str(tmp_path / "home/u")], ["**/.cache"])
    paths = set(lst)
    assert str(tmp_path / "home/u/f.txt") in paths
    assert str(tmp_path / "home/u/link") in paths and lst[str(tmp_path / "home/u/link")].type == "symlink"
    assert not any(".cache" in p for p in paths)


def test_live_listing_under_root_prefix(tmp_path):
    (tmp_path / "sb/home/u").mkdir(parents=True)
    (tmp_path / "sb/home/u/f").write_text("f")
    lst = cmp.listing_from_live(["/home/u"], [], root=str(tmp_path / "sb"))
    assert "/home/u/f" in lst          # keyed by system path, not sandbox path


def test_tree_counts_and_collapse():
    a = L({"/h": ("dir", 0, T), "/h/d": ("dir", 0, T), "/h/d/x": ("file", 1, T)})
    b = L({"/h": ("dir", 0, T), "/h/d": ("dir", 0, T), "/h/d/x": ("file", 2, T + 9),
           "/h/n": ("dir", 0, T), "/h/n/1": ("file", 1, T), "/h/n/2": ("file", 1, T), "/h/n/deep": ("dir", 0, T),
           "/h/n/deep/3": ("file", 1, T)})
    tree = cmp.build_tree(cmp.diff_listings(a, b), ["/h"])
    root = tree.children["/h"]
    assert root.counts["modified"] == 1 and root.counts["added"] == 3    # files, not dir entries
    text = cmp.render_tree(tree, depth=1, color=False)
    assert "+ n/  (+3)" in text                                       # whole-new dir collapsed
    assert "deep" not in text
    assert "deep" in cmp.render_tree(tree, depth=1, show_all=True, color=False)


def test_roots_prefer_profile_paths():
    ch = [cmp.Change("/home/u/x", "added")]
    assert cmp.roots_for(ch, ["/home/u"], ["/home/u"]) == ["/home/u"]
    assert cmp.roots_for(ch, ["/etc"], ["/home/u"]) == ["/home/u"]        # profile doesn't cover: fall back


def test_history_glyph_strip():
    a = L({"/h": ("dir", 0, T), "/h/f": ("file", 1, T)})
    b = L({"/h": ("dir", 0, T), "/h/f": ("file", 2, T + 9)})
    c = L({"/h": ("dir", 0, T)})
    _, tree = cmp.history([a, b, c], roots=["/h"])
    out = cmp.render_history(tree, ["s1", "s2", "s3"])
    assert "[~ -]" in out and "f" in out
