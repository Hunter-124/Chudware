"""End-to-end regression tests for managed removable print media (real temp directories).

These pin the consumer-visible behaviour: after a second revision the printer menu holds
exactly one current file per part, removed parts are retired, superseded bytes are
recoverable from a non-printable ZIP, retries add no revision, unknown files stay and are
flagged, drift refuses without mutation, and failures leave the current set intact.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from services.manufacturing import print_media as pm


def _job(src_dir: Path, part_id: str, body: bytes, *, print_ready: bool = True) -> dict:
    src_dir.mkdir(parents=True, exist_ok=True)
    stem = part_id.replace(" ", "_")
    gcode = src_dir / f"{stem}.gcode"
    gcode.write_bytes(body)
    manifest = {
        "schema": "chudware.print_job/1", "part": part_id, "gcode": str(gcode),
        "gcode_sha256": hashlib.sha256(body).hexdigest(), "print_ready": print_ready,
        "printer": {"name": "Creality Ender-3 (0.4 mm nozzle)"},
        "slicer_warnings": [f"{part_id}: thin wall near boss"],
        "gcode_check": {"status": "pass", "issues": [{"check": "temps", "status": "pass"}]},
        "printability": {"status": "warn",
                         "issues": [{"check": "overhang", "status": "warn", "value": 52}]},
    }
    manifest_path = src_dir / f"{stem}.print.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {"part_id": part_id, "manifest_path": str(manifest_path)}


def _tree(root: Path) -> dict:
    """Every directory and file (with bytes) below ``root``."""
    out: dict = {}
    for base, dirs, files in os.walk(root):
        rel = Path(base).relative_to(root).as_posix()
        out[rel + "/"] = None
        for name in files:
            out[(Path(rel) / name).as_posix()] = (Path(base) / name).read_bytes()
    return out


def _inventory(root: Path, **kw) -> dict:
    inv = pm.inventory_print_media(str(root), **kw)
    assert inv["ok"], inv
    return inv


def _publish(root: Path, project: str, jobs: list, **kw) -> dict:
    token = _inventory(root)["inventory_token"]
    return pm.publish_print_media(str(root), project, jobs, token, media_idle=True, **kw)


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    root = tmp_path / "drive"
    root.mkdir()
    (root / "old_upper_arm.gcode").write_bytes(b"G1 legacy upper arm\n")
    (root / "notes.txt").write_text("keep me", encoding="utf-8")
    return root


def _rev1(tmp_path: Path) -> list:
    src = tmp_path / "rev1"
    return [_job(src, "upper_arm", b"G1 UA v1\n"), _job(src, "lower_arm", b"G1 LA\n"),
            _job(src, "gripper", b"G1 GR\n")]


def _rev2(tmp_path: Path) -> list:
    src = tmp_path / "rev2"
    return [_job(src, "upper_arm", b"G1 UA v2\n"), _job(src, "lower_arm", b"G1 LA\n"),
            _job(src, "wrist", b"G1 WR\n")]


def test_second_revision_leaves_one_current_file_per_part(drive, tmp_path):
    first = _publish(drive, "arm", _rev1(tmp_path))
    assert first["ok"], first
    assert first["revision"] == 1 and first["release"] == "R001"
    assert first["folder"] == "PRINT/ARM"
    assert sorted(e["file_name"] for e in first["current"]) == [
        "R00101_UPPER_ARM.gcode", "R00102_LOWER_ARM.gcode", "R00103_GRIPPER.gcode"]
    # Slicer/check evidence is carried through unchanged, never re-judged.
    upper = next(e for e in first["current"] if e["part_id"] == "upper_arm")
    source = json.loads(Path(_rev1(tmp_path)[0]["manifest_path"]).read_text())
    assert upper["warnings"] == {k: source[k] for k in
                                 ("slicer_warnings", "gcode_check", "printability")}
    assert first["archive"] is None
    assert first["unmanaged_printables"] == ["old_upper_arm.gcode"]
    assert first["reconciled"] is False and first["status"] == "published_unreconciled"

    second = _publish(drive, "arm", _rev2(tmp_path))
    assert second["ok"], second
    assert second["revision"] == 2 and second["changed"] is True

    inv = _inventory(drive)
    current = sorted(r["path"] for r in inv["current_printables"])
    # Stable part numbers: the retired gripper keeps 03, the new wrist gets 04.
    assert current == ["PRINT/ARM/R00201_UPPER_ARM.gcode", "PRINT/ARM/R00202_LOWER_ARM.gcode",
                       "PRINT/ARM/R00204_WRIST.gcode"]
    assert len({Path(p).name[:6].upper() for p in current}) == len(current)
    printable = {r["path"] for r in inv["printable_files"]}
    assert printable == set(current) | {"old_upper_arm.gcode"}, "old revision still printable"
    assert [r["path"] for r in inv["unmanaged_printables"]] == ["old_upper_arm.gcode"]
    assert any(w["code"] == "unmanaged_printables" for w in inv["warnings"])
    for base, _dirs, files in os.walk(drive):  # nothing printable hides in staging/archive
        for name in files:
            rel = (Path(base) / name).relative_to(drive).as_posix()
            if pm._is_printable(name):
                assert rel in printable, rel
    project = inv["managed_projects"][0]
    assert project["project"] == "ARM" and project["revision"] == 2 and project["complete"]

    archive = drive / second["archive"]["path"]
    assert second["archive"]["path"] == "CHUDWARE/ARCHIVE/ARM/ARM_RETIRED_BY_R002.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == second["archive"]["sha256"]
    with zipfile.ZipFile(archive) as zf:
        assert zf.read("PRINT/ARM/R00101_UPPER_ARM.gcode") == b"G1 UA v1\n"
        assert zf.read("PRINT/ARM/R00102_LOWER_ARM.gcode") == b"G1 LA\n"
        assert zf.read("PRINT/ARM/R00103_GRIPPER.gcode") == b"G1 GR\n"
        retired = json.loads(zf.read("RETIRED.json"))
    assert {f["path"] for f in retired["files"]} == {
        "PRINT/ARM/R00101_UPPER_ARM.gcode", "PRINT/ARM/R00102_LOWER_ARM.gcode",
        "PRINT/ARM/R00103_GRIPPER.gcode"}
    assert {r["reason"] for r in second["retired"]} == {"previous_revision"}
    assert inv["archives"] == [{"path": second["archive"]["path"], "project": "ARM",
                                "size": archive.stat().st_size}]

    # Unrelated data is untouched; old active files were moved into the archive, never
    # overwritten in place.
    assert (drive / "notes.txt").read_text() == "keep me"
    assert (drive / "old_upper_arm.gcode").read_bytes() == b"G1 legacy upper arm\n"
    assert not (drive / "PRINT/ARM/R00101_UPPER_ARM.gcode").exists()
    manifest = json.loads((drive / "PRINT/ARM/chudware_manifest.json").read_text())
    assert manifest["revision"] == 2
    assert manifest["part_numbers"] == {"upper_arm": 1, "lower_arm": 2, "gripper": 3, "wrist": 4}
    assert manifest["history"][0]["revision"] == 1
    readme = (drive / "PRINT/ARM/README.txt").read_text()
    assert "R00204_WRIST.gcode" in readme and "R00103" not in readme
    assert not (drive / pm.JOURNAL_PATH).exists()
    assert not (drive / pm.STAGING_DIR).exists()


def test_retry_of_the_same_set_adds_no_revision(drive, tmp_path):
    jobs = _rev1(tmp_path)
    assert _publish(drive, "arm", jobs)["ok"]
    snapshot = _tree(drive)
    again = _publish(drive, "arm", jobs)
    assert again["ok"], again
    assert again["status"].startswith("unchanged") and again["changed"] is False
    assert again["revision"] == 1
    assert _tree(drive) == snapshot


def test_explicit_retire_archives_unknown_file_and_retry_is_idempotent(drive, tmp_path):
    jobs = _rev1(tmp_path)
    result = _publish(drive, "arm", jobs, retire_paths=["old_upper_arm.gcode"])
    assert result["ok"], result
    assert result["status"] == "published" and result["reconciled"] is True
    assert result["unmanaged_printables"] == []
    assert not (drive / "old_upper_arm.gcode").exists()
    with zipfile.ZipFile(drive / result["archive"]["path"]) as zf:
        assert zf.read("old_upper_arm.gcode") == b"G1 legacy upper arm\n"
    assert result["retired"] == [{"path": "old_upper_arm.gcode", "sha256": hashlib.sha256(
        b"G1 legacy upper arm\n").hexdigest(), "size": 20, "reason": "explicit"}]
    snapshot = _tree(drive)
    retry = _publish(drive, "arm", jobs, retire_paths=["old_upper_arm.gcode"])
    assert retry["ok"] and retry["status"] == "unchanged" and retry["revision"] == 1, retry
    assert _tree(drive) == snapshot


def test_drift_since_inventory_refuses_without_mutation(drive, tmp_path):
    jobs = _rev1(tmp_path)
    token = _inventory(drive)["inventory_token"]
    (drive / "copied_from_pc.gcode").write_bytes(b"G1 surprise\n")
    snapshot = _tree(drive)
    result = pm.publish_print_media(str(drive), "arm", jobs, token, media_idle=True)
    assert result["ok"] is False and result["error"] == "inventory_changed", result
    assert result["inventory_token"] != token
    assert _tree(drive) == snapshot


@pytest.mark.parametrize("idle", [False, None, "yes", 1])
def test_publish_requires_explicit_media_idle(drive, tmp_path, idle):
    jobs = _rev1(tmp_path)
    token = _inventory(drive)["inventory_token"]
    snapshot = _tree(drive)
    result = pm.publish_print_media(str(drive), "arm", jobs, token, media_idle=idle)
    assert result["error"] == "media_not_confirmed_idle", result
    assert _tree(drive) == snapshot


def test_modified_managed_file_refuses_until_reviewed(drive, tmp_path):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    tampered = drive / "PRINT/ARM/R00101_UPPER_ARM.gcode"
    tampered.write_bytes(b"G1 edited on the printer\n")
    inv = _inventory(drive)
    row = next(r for r in inv["files"] if r["path"] == "PRINT/ARM/R00101_UPPER_ARM.gcode")
    assert row["status"] == "modified"
    assert any(w["code"] == "managed_file_modified" for w in inv["warnings"])
    snapshot = _tree(drive)
    refused = _publish(drive, "arm", _rev2(tmp_path))
    assert refused["error"] == "managed_file_modified", refused
    assert _tree(drive) == snapshot

    reviewed = _publish(drive, "arm", _rev2(tmp_path),
                        retire_paths=["PRINT/ARM/R00101_UPPER_ARM.gcode"])
    assert reviewed["ok"], reviewed
    with zipfile.ZipFile(drive / reviewed["archive"]["path"]) as zf:
        assert zf.read("PRINT/ARM/R00101_UPPER_ARM.gcode") == b"G1 edited on the printer\n"
    reasons = {r["path"]: r["reason"] for r in reviewed["retired"]}
    assert reasons["PRINT/ARM/R00101_UPPER_ARM.gcode"] == "explicit"
    assert reasons["PRINT/ARM/R00103_GRIPPER.gcode"] == "previous_revision"


def test_failing_staging_leaves_current_set_intact(drive, tmp_path, monkeypatch):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    real = pm._stage_copy
    calls: list = []

    def flaky(src, dst, sha):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(errno.EIO, "simulated write error")
        return real(src, dst, sha)

    monkeypatch.setattr(pm, "_stage_copy", flaky)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert result["ok"] is False and result["error"] == "staging_failed", result
    assert result["current_set_untouched"] is True
    assert _tree(drive) == snapshot
    inv = _inventory(drive)
    assert sorted(r["path"] for r in inv["current_printables"]) == [
        "PRINT/ARM/R00101_UPPER_ARM.gcode", "PRINT/ARM/R00102_LOWER_ARM.gcode",
        "PRINT/ARM/R00103_GRIPPER.gcode"]


def test_swap_failure_rolls_back_to_the_previous_set(drive, tmp_path, monkeypatch):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    token = _inventory(drive)["inventory_token"]
    real = pm._move

    def unplugged(src, dst):
        if dst.name.startswith("R00202"):
            raise OSError(errno.EIO, "simulated unplug")
        real(src, dst)

    monkeypatch.setattr(pm, "_move", unplugged)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert result["ok"] is False and result["error"] == "publish_rolled_back", result
    assert _tree(drive) == snapshot
    inv = _inventory(drive)
    assert inv["inventory_token"] == token
    assert inv["interrupted_publish"] is None and inv["archives"] == []


def test_interrupted_publish_is_reported_then_recovered(drive, tmp_path, monkeypatch):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    real = pm._move

    def unplugged(src, dst):
        if dst.name.startswith("R00202"):
            raise OSError(errno.EIO, "simulated power loss")
        real(src, dst)

    monkeypatch.setattr(pm, "_move", unplugged)
    monkeypatch.setattr(pm, "_rollback", lambda rootp, journal: ["process died"])
    crashed = _publish(drive, "arm", _rev2(tmp_path))
    assert crashed["ok"] is False and crashed["error"] == "rollback_incomplete", crashed
    monkeypatch.undo()

    inv = _inventory(drive)
    pending = inv["interrupted_publish"]
    assert pending["valid"] and pending["phase"] == "swapping" and pending["revision"] == 2
    assert inv["warnings"][0]["code"] == "interrupted_publish"
    assert inv["reconciled"] is False
    # The held old files sit in staging under a non-printable suffix, never as G-code.
    assert inv["staging_leftovers"]
    assert not any(pm._is_printable(p) for p in inv["staging_leftovers"])
    statuses = {j["part_id"]: j["status"] for j in inv["managed_projects"][0]["jobs"]}
    assert statuses["upper_arm"] == "missing"

    # A publish against a stale token still refuses; with the fresh token it reports the
    # recovery explicitly and lands a clean revision.
    recovered = _publish(drive, "arm", _rev2(tmp_path))
    assert recovered["ok"], recovered
    assert recovered["recovered"]["action"] == "rolled_back"
    assert recovered["warnings"][0]["code"] == "recovered_interrupted_publish"
    assert recovered["revision"] == 2
    final = _inventory(drive)
    assert sorted(r["path"] for r in final["current_printables"]) == [
        "PRINT/ARM/R00201_UPPER_ARM.gcode", "PRINT/ARM/R00202_LOWER_ARM.gcode",
        "PRINT/ARM/R00204_WRIST.gcode"]
    assert final["interrupted_publish"] is None and final["staging_leftovers"] == []
    with zipfile.ZipFile(drive / recovered["archive"]["path"]) as zf:
        assert zf.read("PRINT/ARM/R00101_UPPER_ARM.gcode") == b"G1 UA v1\n"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
@pytest.mark.parametrize("phase", ["swapping", "committed"])
def test_recovery_refuses_symlinked_paths_before_mutation(drive, tmp_path, phase):
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "R00101_UPPER_ARM.gcode"
    protected.write_bytes(b"unrelated file outside media")
    (drive / "PRINT").mkdir()
    (drive / "PRINT/ARM").symlink_to(outside, target_is_directory=True)
    (drive / "CHUDWARE").mkdir()
    journal = {
        "schema": pm.SCHEMA_JOURNAL, "txn": "0123456789abcdef", "phase": phase,
        "project": "ARM", "revision": 1, "retire": [], "metadata": [], "archive": None,
        "place": [{"path": "PRINT/ARM/R00101_UPPER_ARM.gcode",
                   "sha256": hashlib.sha256(protected.read_bytes()).hexdigest()}],
    }
    journal_path = drive / pm.JOURNAL_PATH
    journal_path.write_text(json.dumps(journal))
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert not result["ok"] and result["error"] == "media_symlink", result
    assert protected.read_bytes() == b"unrelated file outside media"
    assert json.loads(journal_path.read_text()) == journal


@pytest.mark.parametrize("section", ["place", "metadata"])
def test_recovery_cannot_retire_another_projects_files(drive, tmp_path, section):
    protected = drive / "PRINT/OTHER/R00101_BRACKET.gcode"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"another project")
    (drive / "CHUDWARE").mkdir()
    entry = {"path": protected.relative_to(drive).as_posix(),
             "sha256": hashlib.sha256(protected.read_bytes()).hexdigest()}
    if section == "metadata":
        entry = {"path": entry["path"], "new_sha256": entry["sha256"], "old": None}
    journal = {
        "schema": pm.SCHEMA_JOURNAL, "txn": "0123456789abcdef", "phase": "swapping",
        "project": "ARM", "revision": 1, "retire": [], "metadata": [], "place": [],
        "archive": None,
    }
    journal[section] = [entry]
    (drive / pm.JOURNAL_PATH).write_text(json.dumps(journal))
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert not result["ok"] and result["error"] == "journal_invalid", result
    assert _tree(drive) == snapshot


def test_source_packets_must_be_print_ready_and_hash_exact(drive, tmp_path):
    src = tmp_path / "bad"
    not_ready = _job(src, "bracket", b"G1 BR\n", print_ready=False)
    drifted = _job(src, "clip", b"G1 CL\n")
    Path(json.loads(Path(drifted["manifest_path"]).read_text())["gcode"]).write_bytes(b"G1 X\n")
    snapshot = _tree(drive)
    result = _publish(drive, "arm", [not_ready, drifted, dict(not_ready, extra=1)])
    assert result["error"] == "jobs_invalid", result
    codes = [e["error"] for e in result["job_errors"]]
    assert codes == ["not_print_ready", "gcode_hash_mismatch", "job_invalid"]
    assert result["job_errors"][0]["evidence"]["printability"]["status"] == "warn"
    dup = _job(tmp_path / "dup", "clip", b"G1 CL\n")
    assert _publish(drive, "arm", [dup, dup])["job_errors"][0]["error"] == "duplicate_part_id"
    assert _publish(drive, "arm", [])["error"] == "jobs_invalid"
    assert _tree(drive) == snapshot


def test_missing_root_is_never_created(tmp_path):
    missing = tmp_path / "PRO GAMER D"
    assert pm.inventory_print_media(str(missing))["error"] == "root_missing"
    result = pm.publish_print_media(str(missing), "arm", _rev1(tmp_path), "0" * 64,
                                    media_idle=True)
    assert result["error"] == "root_missing"
    assert not missing.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinks_are_listed_not_followed_and_block_mutation(drive, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.gcode").write_bytes(b"G1 outside\n")
    os.symlink(outside, drive / "linked_dir")
    inv = _inventory(drive)
    row = next(r for r in inv["files"] if r["path"] == "linked_dir")
    assert row["type"] == "symlink" and row["target"] == str(outside)
    assert not any(r["path"].startswith("linked_dir/") for r in inv["files"])

    os.symlink(outside, drive / "PRINT")
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert result["error"] == "media_symlink", result
    assert sorted(p.name for p in outside.iterdir()) == ["secret.gcode"]


def test_pagination_keeps_a_whole_drive_token(drive):
    for index in range(25):
        (drive / f"file_{index:02d}.txt").write_text(str(index))
    full = _inventory(drive, limit=1000)
    seen = []
    offset = 0
    while True:
        page = _inventory(drive, offset=offset, limit=7)
        assert page["inventory_token"] == full["inventory_token"]
        assert page["total"] == full["total"]
        assert page["printable_files"] == full["printable_files"]
        seen += [r["path"] for r in page["files"]]
        if not page["truncated"]:
            break
        offset += 7
    assert seen == [r["path"] for r in full["files"]]
    assert pm.inventory_print_media(str(drive), limit=0)["error"] == "page_invalid"


def test_inventory_shows_directories_duplicates_and_name_hints(drive):
    (drive / "old").mkdir()
    (drive / "old" / "upper_arm_v2.gcode").write_bytes(b"G1 legacy upper arm\n")
    inv = _inventory(drive)
    kinds = {r["path"]: r["type"] for r in inv["files"]}
    assert kinds["old"] == "dir" and kinds["notes.txt"] == "file"
    assert inv["duplicate_groups"][0]["files"] == [
        {"path": "old/upper_arm_v2.gcode", "status": "unmanaged"},
        {"path": "old_upper_arm.gcode", "status": "unmanaged"}]
    assert any(w["code"] == "duplicate_printables" for w in inv["warnings"])
    assert all(len(r["sha256"]) == 64 for r in inv["printable_files"])


def test_retire_paths_are_validated(drive, tmp_path):
    jobs = _rev1(tmp_path)
    for bad, code in (("../escape.gcode", "path_traversal"), ("/abs.gcode", "path_traversal"),
                      ("nope.gcode", "retire_path_missing"),
                      ("notes.txt", "retire_path_not_printable")):
        snapshot = _tree(drive)
        result = _publish(drive, "arm", jobs, retire_paths=[bad])
        assert result["error"] == code, (bad, result)
        assert _tree(drive) == snapshot


def test_retire_paths_cannot_reach_into_another_project(drive, tmp_path):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    base = [_job(tmp_path / "base", "plate", b"G1 PL\n")]
    snapshot = _tree(drive)
    result = _publish(drive, "base", base, retire_paths=["PRINT/ARM/R00101_UPPER_ARM.gcode"])
    assert result["error"] == "retire_path_managed_elsewhere" and result["project"] == "ARM"
    assert _tree(drive) == snapshot
    ok = _publish(drive, "base", base)
    assert ok["ok"] and ok["current"][0]["path"] == "PRINT/BASE/R00101_PLATE.gcode", ok
    projects = {p["project"]: p["complete"] for p in _inventory(drive)["managed_projects"]}
    assert projects == {"ARM": True, "BASE": True}


def test_loose_file_in_project_folder_must_be_reviewed(drive, tmp_path):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    (drive / "PRINT/ARM/hand_copied.gcode").write_bytes(b"G1 hand\n")
    refused = _publish(drive, "arm", _rev2(tmp_path))
    assert refused["error"] == "unmanaged_in_project_folder"
    assert refused["paths"] == ["PRINT/ARM/hand_copied.gcode"]
    ok = _publish(drive, "arm", _rev2(tmp_path), retire_paths=["PRINT/ARM/hand_copied.gcode"])
    assert ok["ok"], ok
    assert not (drive / "PRINT/ARM/hand_copied.gcode").exists()


def test_revision_limit_is_an_explicit_refusal(drive, tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MAX_REVISION", 1)
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert result["error"] == "revision_limit", result
    assert _tree(drive) == snapshot


def test_stale_staging_is_ignored_by_token_and_cleaned(drive, tmp_path):
    (drive / "CHUDWARE").mkdir()
    token = _inventory(drive)["inventory_token"]
    leftover = drive / pm.STAGING_DIR / "deadbeefdeadbeef"
    leftover.mkdir(parents=True)
    (leftover / f"new-00{pm.STAGED_SUFFIX}").write_bytes(b"G1 partial")
    inv = _inventory(drive)
    assert inv["inventory_token"] == token
    assert any(w["code"] == "stale_staging" for w in inv["warnings"])
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    assert not (drive / pm.STAGING_DIR).exists()


@pytest.mark.skipif(pm.fcntl is None, reason="POSIX flock lock")
def test_concurrent_publish_is_refused_while_the_drive_is_locked(drive, tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "LOCK_WAIT_S", 0.1)
    token = _inventory(drive)["inventory_token"]
    fd = os.open(drive, os.O_RDONLY | os.O_DIRECTORY)
    try:
        pm.fcntl.flock(fd, pm.fcntl.LOCK_EX)
        inv = _inventory(drive)
        assert inv["busy"] is True and inv["warnings"][0]["code"] == "publish_in_progress"
        result = pm.publish_print_media(str(drive), "arm", _rev1(tmp_path), token,
                                        media_idle=True)
        assert result["error"] == "media_busy", result
    finally:
        os.close(fd)
    assert not (drive / "PRINT").exists()
