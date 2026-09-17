"""Unit tests for the operation journal and snapshot annotation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from porta_winux import ops  # noqa: E402
from porta_winux.manifest import DriveLayout  # noqa: E402
from porta_winux.store import Snapshot  # noqa: E402


def snap(id_, tags, host="h"):
    return Snapshot(id=id_ * 8, short_id=id_, time="2026-01-01T00:00:00Z", hostname=host,
                    tags=tags, paths=["/home"])


def test_journal_roundtrip_and_undo_pointer(tmp_path):
    lo = DriveLayout(root=tmp_path)
    with ops.Operation(lo, "sync", force=True, force_reason="overwrite-conflicts",
                       args={"commits": ["c1"]}) as op:
        op.record(safety="safe1234" * 8)
        op.record(result="resu5678" * 8)
        op.note("overwrote 1 file")
    recs = ops.read_journal(lo)
    assert len(recs) == 1
    r = recs[0]
    assert r["op"] == op.id and r["status"] == "ok" and r["forced"]
    assert r["snapshots"]["safety"].startswith("safe1234")
    assert r["notes"] == ["overwrote 1 file"]
    assert set(op.tags()) == {f"op:{op.id}", "via:sync", "forced"}
    assert "safety" in op.safety_tags() and "pre:sync" in op.safety_tags()

    rows = ops.annotate([snap("safe1234", op.safety_tags()), snap("resu5678", ["system"] + op.tags())], recs)
    assert rows[0]["kind"] == "safety" and rows[0]["pre"] == "sync" and rows[0]["undo"] is None
    assert rows[1]["kind"] == "system" and rows[1]["forced"]
    assert rows[1]["undo"].startswith("safe1234")


def test_journal_records_errors_and_unfinished(tmp_path):
    lo = DriveLayout(root=tmp_path)
    try:
        with ops.Operation(lo, "restore"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # an op that never reached __exit__ (yank): fake a lone begin line
    with open(lo.journal_path, "a") as f:
        f.write('{"event": "begin", "op": "deadbe", "cmd": "snapshot", "time": "t", "host": "h"}\n')
    recs = ops.read_journal(lo)
    assert recs[0]["status"] == "error" and "boom" in recs[0]["error"]
    assert recs[1]["status"] == "unfinished"
    assert "UNFINISHED" in ops.format_journal_line(recs[1])


def test_journal_missing_is_empty(tmp_path):
    assert ops.read_journal(DriveLayout(root=tmp_path)) == []


def test_signature_is_idempotent(tmp_path):
    lo = DriveLayout(root=tmp_path)
    assert not lo.is_drive()
    s1 = lo.write_signature("0.4.0")
    s2 = lo.write_signature("9.9.9")
    assert s1["drive_id"] == s2["drive_id"] and s2["created_with"] == "porta-winux 0.4.0"
    assert lo.is_drive() and lo.read_signature()["format"] == 1
