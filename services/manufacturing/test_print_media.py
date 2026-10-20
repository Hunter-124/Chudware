"""End-to-end regression tests for managed removable print media (real temp directories).

These pin the consumer-visible behaviour: after a second revision the printer menu holds
exactly one current file per part, removed parts and superseded bytes are deleted,
retries add no revision, unknown files stay and are flagged, drift refuses without
mutation, and failures leave the current set intact.
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


def _job(src_dir: Path, part_id: str, body: bytes, *, print_ready: bool = True,
         suffix: str = ".gcode") -> dict:
    src_dir.mkdir(parents=True, exist_ok=True)
    stem = part_id.replace(" ", "_")
    gcode = src_dir / f"{stem}{suffix}"
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



def _assert_delivery(root: Path, expected: dict[str, bytes], *, folders: set[str] = frozenset()):
    """Printer-visible files are exactly the requested paths, with verified bytes."""
    actual = {path.relative_to(root).as_posix(): path.read_bytes()
              for path in root.rglob("*")
              if path.is_file() and path.suffix.lower() in {".gcode", ".gco", ".g", ".bgcode"}}
    assert actual == expected
    assert {path.relative_to(root).as_posix() for path in root.rglob("*")
            if path.is_dir()} == folders
    inv = _inventory(root)
    for entry in inv["current_printables"]:
        assert entry["sha256"] == hashlib.sha256(expected[entry["path"]]).hexdigest()
    assert inv["interrupted_publish"] is None
    assert inv["staging_leftovers"] == []


def _assert_no_archives(root: Path):
    assert not [path.relative_to(root).as_posix() for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() == ".zip"]


def _assert_absent_bytes(root: Path, *retired_bodies: bytes):
    """Neither hidden files nor non-printable staging may retain retired toolpaths."""
    for path in root.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            assert not any(retired in body for retired in retired_bodies), path


def _legacy_project(root: Path, *, stale: bool = True) -> dict[str, bytes]:
    """A previously published drive, including a managed stale printable revision."""
    folder = root / "PRINT/ARM"
    folder.mkdir(parents=True)
    bodies = {"PRINT/ARM/R00201_UPPER_ARM.gcode": b"G1 UA v1\n",
              "PRINT/ARM/R00202_LOWER_ARM.gcode": b"G1 LA\n",
              "PRINT/ARM/R00203_GRIPPER.gcode": b"G1 GR\n"}
    if stale:
        bodies["PRINT/ARM/R00101_UPPER_ARM.gcode"] = b"G1 UA older\n"
    for path, body in bodies.items():
        (root / path).write_bytes(body)
    current = [{"part_id": part_id, "part_number": index,
                "path": f"PRINT/ARM/R002{index:02d}_{part_id.upper()}.gcode",
                "file_name": f"R002{index:02d}_{part_id.upper()}.gcode",
                "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
               for index, (part_id, body) in enumerate(
                   [("upper_arm", b"G1 UA v1\n"), ("lower_arm", b"G1 LA\n"),
                    ("gripper", b"G1 GR\n")], start=1)]
    old_path = "PRINT/ARM/R00101_UPPER_ARM.gcode"
    readme = b"Legacy managed print release R002\n"
    (folder / "README.txt").write_bytes(readme)
    manifest = {"schema": "chudware.print_media/1", "project": "ARM", "revision": 2,
                "current": current, "part_numbers": {"upper_arm": 1, "lower_arm": 2, "gripper": 3},
                "history": ([{"revision": 1, "files": [
                    {"part_id": "upper_arm", "path": old_path,
                     "sha256": hashlib.sha256(bodies[old_path]).hexdigest()}]}] if stale else []),
                "retired": [], "readme_sha256": hashlib.sha256(readme).hexdigest()}
    (folder / "chudware_manifest.json").write_text(json.dumps(manifest))
    return bodies


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
    jobs = _rev1(tmp_path)
    first = _publish(drive, "arm", jobs)
    assert first["ok"], first
    assert first["revision"] == 1 and first["release"] == "R001"
    assert first["folder"] == "."
    assert sorted(e["file_name"] for e in first["current"]) == [
        "gripper.gcode", "lower_arm.gcode", "upper_arm.gcode"]
    # Slicer/check evidence is carried through unchanged, never re-judged.
    upper = next(e for e in first["current"] if e["part_id"] == "upper_arm")
    source = json.loads(Path(jobs[0]["manifest_path"]).read_text())
    assert upper["warnings"] == {k: source[k] for k in
                                 ("slicer_warnings", "gcode_check", "printability")}
    assert "archive" not in first
    assert first["unmanaged_printables"] == ["old_upper_arm.gcode"]
    assert first["reconciled"] is False

    second = _publish(drive, "arm", _rev2(tmp_path))
    assert second["ok"], second
    assert second["revision"] == 2 and second["changed"] is True
    inv = _inventory(drive)
    current = sorted(r["path"] for r in inv["current_printables"])
    assert current == ["lower_arm.gcode", "upper_arm.gcode", "wrist.gcode"]
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v2\n", "lower_arm.gcode": b"G1 LA\n",
                             "wrist.gcode": b"G1 WR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
    assert [r["path"] for r in inv["unmanaged_printables"]] == ["old_upper_arm.gcode"]
    assert any(w["code"] == "unmanaged_printables" for w in inv["warnings"])
    project = inv["managed_projects"][0]
    assert project["project"] == "ARM" and project["revision"] == 2 and project["complete"]
    assert "archive" not in second
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 UA v1\n", b"G1 GR\n")
    assert {r["reason"] for r in second["retired"]} == {"previous_revision"}
    assert inv["archives"] == []
    assert (drive / "notes.txt").read_text() == "keep me"
    assert not (drive / "gripper.gcode").exists()
    manifest = json.loads((drive / ".chudware_ARM_manifest.json").read_text())
    assert manifest["revision"] == 2
    assert manifest["history"][0]["revision"] == 1
    assert {e["path"] for e in manifest["current"]} == set(current)


def test_retry_of_the_same_set_adds_no_revision(drive, tmp_path):
    jobs = _rev1(tmp_path)
    assert _publish(drive, "arm", jobs)["ok"]
    snapshot = _tree(drive)
    again = _publish(drive, "arm", jobs)
    assert again["ok"], again
    assert again["status"].startswith("unchanged") and again["changed"] is False
    assert again["revision"] == 1
    assert _tree(drive) == snapshot


def test_explicit_retire_deletes_unknown_file_and_retry_is_idempotent(drive, tmp_path):
    jobs = _rev1(tmp_path)
    result = _publish(drive, "arm", jobs, retire_paths=["old_upper_arm.gcode"])
    assert result["ok"], result
    assert result["status"] == "published" and result["reconciled"] is True
    assert result["unmanaged_printables"] == []
    assert not (drive / "old_upper_arm.gcode").exists()
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 legacy upper arm\n")
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
    tampered = drive / "upper_arm.gcode"
    tampered.write_bytes(b"G1 edited on the printer\n")
    inv = _inventory(drive)
    row = next(r for r in inv["files"] if r["path"] == "upper_arm.gcode")
    assert row["status"] == "modified"
    assert any(w["code"] == "managed_file_modified" for w in inv["warnings"])
    snapshot = _tree(drive)
    refused = _publish(drive, "arm", _rev2(tmp_path))
    assert refused["error"] == "managed_file_modified", refused
    assert _tree(drive) == snapshot

    reviewed = _publish(drive, "arm", _rev2(tmp_path),
                        retire_paths=["upper_arm.gcode"])
    assert reviewed["ok"], reviewed
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 edited on the printer\n")
    reasons = {r["path"]: r["reason"] for r in reviewed["retired"]}
    assert reasons["upper_arm.gcode"] == "explicit"
    assert reasons["gripper.gcode"] == "previous_revision"


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
        "gripper.gcode", "lower_arm.gcode", "upper_arm.gcode"]


@pytest.mark.parametrize("failed_name", ["upper_arm.gcode", "wrist.gcode"])
def test_swap_failure_rolls_back_to_the_previous_set(drive, tmp_path, monkeypatch, failed_name):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    token = _inventory(drive)["inventory_token"]
    real = pm._move
    failed = False

    def unplugged(src, dst):
        nonlocal failed
        if dst.name == failed_name and not failed:
            failed = True
            raise OSError(errno.EIO, "simulated unplug")
        real(src, dst)

    monkeypatch.setattr(pm, "_move", unplugged)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert result["ok"] is False and result["error"] == "publish_rolled_back", result
    assert failed, "fault must interrupt an actual stable-name placement"
    assert _tree(drive) == snapshot
    inv = _inventory(drive)
    assert inv["inventory_token"] == token
    assert inv["interrupted_publish"] is None and inv["archives"] == []


def test_interrupted_publish_is_reported_then_recovered(drive, tmp_path, monkeypatch):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    old_token = _inventory(drive)["inventory_token"]
    real = pm._move

    def unplugged(src, dst):
        if dst.name == "wrist.gcode":
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
    assert any(w["code"] == "interrupted_publish" for w in inv["warnings"])
    assert inv["reconciled"] is False
    # Held old bytes are not separate printable revisions.
    assert inv["staging_leftovers"]
    assert not any(Path(p).suffix.lower() in {".gcode", ".gco", ".g", ".bgcode"}
                   for p in inv["staging_leftovers"])
    interrupted_tree = _tree(drive)
    stale = pm.publish_print_media(str(drive), "arm", _rev2(tmp_path), old_token,
                                  media_idle=True)
    assert stale["error"] == "inventory_changed", stale
    assert _tree(drive) == interrupted_tree

    recovered = _publish(drive, "arm", _rev2(tmp_path))
    assert recovered["ok"], recovered
    assert recovered["recovered"]["action"] == "rolled_back"
    assert any(w["code"] == "recovered_interrupted_publish" for w in recovered["warnings"])
    assert recovered["revision"] == 2
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v2\n", "lower_arm.gcode": b"G1 LA\n",
                             "wrist.gcode": b"G1 WR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 UA v1\n", b"G1 GR\n")


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
    journal_path = drive / "CHUDWARE/JOURNAL.json"
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
    (drive / "CHUDWARE/JOURNAL.json").write_text(json.dumps(journal))
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


def test_user_override_publishes_a_not_ready_job_with_its_failed_checks(drive, tmp_path):
    src = tmp_path / "override"
    ready = _job(src, "base", b"G1 BASE\n")
    forearm = _job(src, "forearm", b"G1 FA\n", print_ready=False)
    manifest_path = Path(forearm["manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    manifest["effective"] = {"support_material": 1}
    manifest["gcode_check"]["issues"].append({"check": "floating_extrusion", "status": "fail"})
    manifest["printability"]["issues"].append({"check": "overhang", "status": "fail"})
    manifest_path.write_text(json.dumps(manifest))
    words = "put it on my flashdrive anyway"
    snapshot = _tree(drive)
    for bad in ("", "   ", 7, "x" * 501):
        refused = _publish(drive, "arm", [ready, dict(forearm, user_override=bad)])
        assert refused["job_errors"][0]["error"] == "user_override_invalid", refused
    drifted = _job(tmp_path / "drift", "clip", b"G1 CL\n", print_ready=False)
    Path(json.loads(Path(drifted["manifest_path"]).read_text())["gcode"]).write_bytes(b"G1 X\n")
    refused = _publish(drive, "arm", [dict(drifted, user_override=words)])
    assert refused["job_errors"][0]["error"] == "gcode_hash_mismatch", refused
    assert _tree(drive) == snapshot

    result = _publish(drive, "arm", [ready, dict(forearm, user_override=words)])
    assert result["ok"] and result["status"].startswith("published"), result
    expected = {"user_override": words, "failed_checks": ["gcode_check.floating_extrusion"]}
    by_part = {e["part_id"]: e for e in result["current"]}
    assert by_part["forearm"]["override"] == expected
    assert "override" not in by_part["base"]
    flagged = [w for w in result["warnings"] if w["code"] == "published_not_print_ready"]
    assert [(w["part_id"], w["path"]) for w in flagged] == [("forearm", "forearm.gcode")]
    assert (drive / "forearm.gcode").read_bytes() == b"G1 FA\n"
    readme = (drive / ".chudware_ARM_README.txt").read_text()
    assert "NOT print-ready (failed: gcode_check.floating_extrusion)" in readme and words in readme
    jobs = {j["part_id"]: j for j in _inventory(drive)["managed_projects"][0]["jobs"]}
    assert jobs["forearm"]["override"] == expected and "override" not in jobs["base"]
    again = _publish(drive, "arm", [ready, dict(forearm, user_override=words)])
    assert again["status"].startswith("unchanged"), again
    assert [w["code"] for w in again["warnings"]].count("published_not_print_ready") == 1


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
    result = _publish(drive, "base", base, retire_paths=["upper_arm.gcode"])
    assert result["error"] == "retire_path_managed_elsewhere" and result["project"] == "ARM"
    assert _tree(drive) == snapshot
    ok = _publish(drive, "base", base)
    assert ok["ok"] and ok["current"][0]["path"] == "plate.gcode", ok
    projects = {p["project"]: p["complete"] for p in _inventory(drive)["managed_projects"]}
    assert projects == {"ARM": True, "BASE": True}


def test_loose_file_in_project_folder_must_be_reviewed(drive, tmp_path):
    assert _publish(drive, "arm", _rev1(tmp_path), layout="project")["ok"]
    (drive / "ARM/hand_copied.gcode").write_bytes(b"G1 hand\n")
    snapshot = _tree(drive)
    refused = _publish(drive, "arm", _rev2(tmp_path), layout="project")
    assert refused["error"] == "unmanaged_in_project_folder"
    assert refused["paths"] == ["ARM/hand_copied.gcode"]
    assert _tree(drive) == snapshot
    ok = _publish(drive, "arm", _rev2(tmp_path), layout="project",
                  retire_paths=["ARM/hand_copied.gcode"])
    assert ok["ok"], ok
    _assert_delivery(drive, {"ARM/upper_arm.gcode": b"G1 UA v2\n",
                             "ARM/lower_arm.gcode": b"G1 LA\n", "ARM/wrist.gcode": b"G1 WR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"}, folders={"ARM"})
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 hand\n")


def test_revision_limit_is_an_explicit_refusal(drive, tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MAX_REVISION", 1)
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert result["error"] == "revision_limit", result
    assert _tree(drive) == snapshot


@pytest.mark.parametrize("legacy", [False, True])
def test_stale_staging_is_ignored_by_token_and_cleaned(drive, tmp_path, legacy):
    token = _inventory(drive)["inventory_token"]
    rel = ("CHUDWARE/.staging/deadbeefdeadbeef" if legacy
           else ".chudware_staging_deadbeefdeadbeef")
    leftover = drive / rel
    leftover.mkdir(parents=True)
    (leftover / "new-00.part").write_bytes(b"G1 partial")
    inv = _inventory(drive)
    assert inv["inventory_token"] == token
    assert any(w["code"] == "stale_staging" for w in inv["warnings"])
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v1\n", "lower_arm.gcode": b"G1 LA\n",
                             "gripper.gcode": b"G1 GR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})


@pytest.mark.skipif(pm.fcntl is None, reason="POSIX flock lock")
def test_concurrent_publish_is_refused_while_the_drive_is_locked(drive, tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "LOCK_WAIT_S", 0.1)
    token = _inventory(drive)["inventory_token"]
    snapshot = _tree(drive)
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
    assert _tree(drive) == snapshot


def test_root_delivery_uses_lowercase_stable_part_names(drive, tmp_path):
    jobs = [_job(tmp_path / "src", "Base Plate", b"G1 BASE\n"),
            _job(tmp_path / "src", "Tool-Mount", b"G1 TOOL\n")]
    result = _publish(drive, "robot", jobs)
    assert result["ok"], result
    assert result["folder"] == "."
    assert {entry["part_id"]: entry["path"] for entry in result["current"]} == {
        "Base Plate": "base_plate.gcode", "Tool-Mount": "tool_mount.gcode"}
    _assert_delivery(drive, {"base_plate.gcode": b"G1 BASE\n", "tool_mount.gcode": b"G1 TOOL\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
    assert (drive / ".chudware_ROBOT_manifest.json").is_file()


def test_project_layout_has_only_one_top_level_delivery_folder(drive, tmp_path):
    first = _publish(drive, "arm", _rev1(tmp_path), layout="project")
    assert first["ok"] and first["folder"] == "ARM", first
    result = _publish(drive, "arm", _rev2(tmp_path), layout="project")
    assert result["ok"], result
    assert result["folder"] == "ARM"
    _assert_delivery(drive, {"ARM/upper_arm.gcode": b"G1 UA v2\n",
                             "ARM/lower_arm.gcode": b"G1 LA\n", "ARM/wrist.gcode": b"G1 WR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"}, folders={"ARM"})
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 UA v1\n", b"G1 GR\n")
    assert (drive / ".chudware_ARM_manifest.json").is_file()


@pytest.mark.parametrize("layout", ["nested", "ROOT", "", None, 1, True])
def test_invalid_layout_refuses_without_mutation(drive, tmp_path, layout):
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev1(tmp_path), layout=layout)
    assert not result["ok"] and result["error"] == "layout_invalid", result
    assert _tree(drive) == snapshot


@pytest.mark.parametrize("layout", ["root", "project"])
@pytest.mark.parametrize("stale", [False, True])
def test_byte_identical_legacy_jobs_migrate_and_retire_stale_revisions(
        drive, tmp_path, layout, stale):
    legacy = _legacy_project(drive, stale=stale)
    before = _inventory(drive)
    assert before["managed_projects"][0]["complete"]
    result = _publish(drive, "arm", _rev1(tmp_path), layout=layout)
    assert result["ok"] and result["changed"], result
    assert result["revision"] == 3
    prefix = "" if layout == "root" else "ARM/"
    _assert_delivery(drive, {prefix + "upper_arm.gcode": b"G1 UA v1\n",
                             prefix + "lower_arm.gcode": b"G1 LA\n",
                             prefix + "gripper.gcode": b"G1 GR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"},
                     folders=set() if layout == "root" else {"ARM"})
    assert not any((drive / path).exists() for path in legacy)
    _assert_no_archives(drive)
    if stale:
        _assert_absent_bytes(drive, b"G1 UA older\n")
    assert not (drive / "PRINT").exists()
    assert (drive / ".chudware_ARM_manifest.json").is_file()
    snapshot = _tree(drive)
    retry = _publish(drive, "arm", _rev1(tmp_path), layout=layout)
    assert retry["ok"] and not retry["changed"] and retry["revision"] == 3, retry
    assert _tree(drive) == snapshot


def test_legacy_migration_keeps_unrelated_files_and_nonempty_directories(drive, tmp_path):
    legacy = _legacy_project(drive, stale=False)
    unrelated = {"PRINT/ARM/operator_notes.txt": b"keep notes",
                 "PRINT/OTHER/foreign.gcode": b"G1 foreign\n",
                 "CHUDWARE/vendor_data.bin": b"keep vendor data"}
    for path, body in unrelated.items():
        target = drive / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert result["ok"], result
    for path, body in unrelated.items():
        assert (drive / path).read_bytes() == body
    assert not (drive / "PRINT/ARM/chudware_manifest.json").exists()
    assert not (drive / "PRINT/ARM/README.txt").exists()
    assert not any((drive / path).exists() for path in legacy)
    _assert_no_archives(drive)
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v1\n", "lower_arm.gcode": b"G1 LA\n",
                             "gripper.gcode": b"G1 GR\n", "PRINT/OTHER/foreign.gcode": b"G1 foreign\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"},
                     folders={"PRINT", "PRINT/ARM", "PRINT/OTHER", "CHUDWARE"})


@pytest.mark.parametrize("initial_layout", ["root", "project"])
def test_layout_switch_migrates_byte_identical_jobs_without_printable_aliases(
        drive, tmp_path, initial_layout):
    jobs = _rev1(tmp_path)
    first = _publish(drive, "arm", jobs, layout=initial_layout)
    assert first["ok"], first
    layout = "project" if initial_layout == "root" else "root"
    result = _publish(drive, "arm", jobs, layout=layout)
    assert result["ok"] and result["changed"] and result["revision"] == 2, result
    prefix = "" if layout == "root" else "ARM/"
    _assert_delivery(drive, {prefix + "upper_arm.gcode": b"G1 UA v1\n",
                             prefix + "lower_arm.gcode": b"G1 LA\n",
                             prefix + "gripper.gcode": b"G1 GR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"},
                     folders=set() if layout == "root" else {"ARM"})
    old_prefix = "" if initial_layout == "root" else "ARM/"
    assert not any((drive / (old_prefix + name)).exists()
                   for name in ("upper_arm.gcode", "lower_arm.gcode", "gripper.gcode"))
    _assert_no_archives(drive)


def test_root_projects_coexist_without_cross_retiring(drive, tmp_path):
    arm_jobs = _rev1(tmp_path)
    assert _publish(drive, "arm", arm_jobs)["ok"]
    base = [_job(tmp_path / "base", "base", b"G1 BASE\n")]
    assert _publish(drive, "robot", base)["ok"]
    robot_manifest = (drive / ".chudware_ROBOT_manifest.json").read_bytes()
    second = _publish(drive, "arm", _rev2(tmp_path))
    assert second["ok"], second
    assert (drive / ".chudware_ROBOT_manifest.json").read_bytes() == robot_manifest
    assert _publish(drive, "robot", base)["changed"] is False
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v2\n", "lower_arm.gcode": b"G1 LA\n",
                             "wrist.gcode": b"G1 WR\n", "base.gcode": b"G1 BASE\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
    projects = {p["project"]: p["complete"] for p in _inventory(drive)["managed_projects"]}
    assert projects == {"ARM": True, "ROBOT": True}
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 UA v1\n", b"G1 GR\n")


@pytest.mark.parametrize("same_bytes", [False, True])
def test_root_project_name_collision_never_overwrites_other_project(drive, tmp_path, same_bytes):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    body = b"G1 UA v1\n" if same_bytes else b"G1 other project\n"
    collision = [_job(tmp_path / "other", "upper_arm", body)]
    snapshot = _tree(drive)
    refused = _publish(drive, "robot", collision)
    assert not refused["ok"] and refused["error"] == "destination_exists", refused
    assert _tree(drive) == snapshot
    reviewed = _publish(drive, "robot", collision, retire_paths=["upper_arm.gcode"])
    assert reviewed["error"] == "retire_path_managed_elsewhere", reviewed
    assert _tree(drive) == snapshot
    separate = _publish(drive, "robot", collision, layout="project")
    assert separate["ok"], separate
    assert (drive / "upper_arm.gcode").read_bytes() == b"G1 UA v1\n"
    assert (drive / "ROBOT/upper_arm.gcode").read_bytes() == body


@pytest.mark.parametrize("collision_name", ["base.gcode", "BASE.GCODE"])
def test_root_destination_collision_protects_unknown_files(drive, tmp_path, collision_name):
    (drive / collision_name).write_bytes(b"unknown source\n")
    job = [_job(tmp_path / "src", "base", b"G1 BASE\n")]
    snapshot = _tree(drive)
    result = _publish(drive, "robot", job)
    assert not result["ok"] and result["error"] == "destination_exists", result
    assert _tree(drive) == snapshot
    reviewed = _publish(drive, "robot", job, retire_paths=[collision_name])
    assert reviewed["ok"], reviewed
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"unknown source\n")
    _assert_delivery(drive, {"base.gcode": b"G1 BASE\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})


def test_slug_collisions_refuse_before_writing_any_files(drive, tmp_path):
    jobs = [_job(tmp_path / "a", "base-plate", b"G1 A\n"),
            _job(tmp_path / "b", "base_plate", b"G1 B\n")]
    snapshot = _tree(drive)
    result = _publish(drive, "arm", jobs)
    assert not result["ok"], result
    assert _tree(drive) == snapshot


def test_retirement_failure_preserves_unmoved_byte_identical_destination(drive, tmp_path, monkeypatch):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    real = pm._move
    failed = False

    def unplugged(src, dst):
        nonlocal failed
        if src == drive / "lower_arm.gcode" and dst.suffix == ".part" and not failed:
            failed = True
            raise OSError(errno.EIO, "simulated unplug before holding old file")
        real(src, dst)

    monkeypatch.setattr(pm, "_move", unplugged)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert not result["ok"] and result["error"] == "publish_rolled_back", result
    assert failed, "fault must interrupt retirement before the old stable file is moved"
    assert _tree(drive) == snapshot


def test_legacy_interrupted_publish_recovers_then_migrates(drive, tmp_path):
    legacy = _legacy_project(drive, stale=False)
    txn = "0123456789abcdef"
    held = f"CHUDWARE/.staging/{txn}/old-00.part"
    (drive / held).parent.mkdir(parents=True)
    old_path = "PRINT/ARM/R00201_UPPER_ARM.gcode"
    (drive / old_path).replace(drive / held)
    new_path = "PRINT/ARM/R00301_UPPER_ARM.gcode"
    (drive / new_path).write_bytes(b"G1 UA v2\n")
    journal = {
        "schema": "chudware.print_media_txn/1", "txn": txn, "phase": "swapping",
        "project": "ARM", "revision": 3, "metadata": [], "archive": None,
        "retire": [{"path": old_path, "held": held,
                    "sha256": hashlib.sha256(legacy[old_path]).hexdigest()}],
        "place": [{"path": new_path, "sha256": hashlib.sha256(b"G1 UA v2\n").hexdigest()}]}
    (drive / "CHUDWARE/JOURNAL.json").write_text(json.dumps(journal))
    inv = _inventory(drive)
    assert inv["interrupted_publish"]["valid"]
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert result["ok"], result
    assert result["recovered"]["action"] == "rolled_back"
    assert not any((drive / path).exists() for path in legacy)
    _assert_no_archives(drive)
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v1\n", "lower_arm.gcode": b"G1 LA\n",
                             "gripper.gcode": b"G1 GR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})


@pytest.mark.parametrize("layout", ["root", "project"])
@pytest.mark.parametrize("suffix", [".gcode", ".gco", ".g", ".bgcode"])
def test_supported_print_files_are_delivered_raw_and_hash_exact(drive, tmp_path, layout, suffix):
    body = b"GCDE\x00\x01\xfe\xff" if suffix == ".bgcode" else b"G1 BASE\n"
    job = _job(tmp_path / "src", "base", body, suffix=suffix)
    result = _publish(drive, "robot", [job], layout=layout)
    assert result["ok"], result
    entry = result["current"][0]
    prefix = "" if layout == "root" else "ROBOT/"
    assert entry["path"].startswith(prefix + "base.")
    if suffix == ".bgcode":
        assert entry["path"] == prefix + "base.bgcode"
    assert (drive / entry["path"]).read_bytes() == body
    assert entry["sha256"] == hashlib.sha256(body).hexdigest()
    _assert_delivery(drive, {entry["path"]: body,
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"},
                     folders=set() if layout == "root" else {"ROBOT"})


@pytest.mark.parametrize("legacy", [False, True])
def test_target_archives_are_deleted_but_unknown_zips_and_other_projects_remain(
        drive, tmp_path, legacy):
    jobs = _rev1(tmp_path)
    if legacy:
        _legacy_project(drive, stale=False)
    else:
        assert _publish(drive, "arm", jobs)["ok"]
    target_paths = {".chudware_ARM_RETIRED_BY_R001.zip",
                    "CHUDWARE/ARCHIVE/ARM/ARM_RETIRED_BY_R002.zip"}
    protected_paths = {"operator_backup.zip", ".chudware_OTHER_RETIRED_BY_R001.zip",
                       "CHUDWARE/ARCHIVE/OTHER/OTHER_RETIRED_BY_R002.zip",
                       "CHUDWARE/ARCHIVE/ARM/operator_backup.zip"}
    for path in target_paths | protected_paths:
        archive = drive / path
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("old_upper_arm.gcode", b"G1 archived upper\n")
    protected = {path: (drive / path).read_bytes() for path in protected_paths}
    result = _publish(drive, "arm", jobs)
    assert result["ok"], result
    assert result["revision"] == (3 if legacy else 1)
    assert not any((drive / path).exists() for path in target_paths)
    assert {path.relative_to(drive).as_posix() for path in drive.rglob("*")
            if path.is_file() and path.suffix.lower() == ".zip"} == protected_paths
    for path, body in protected.items():
        assert (drive / path).read_bytes() == body
    assert {entry["path"] for entry in result["current"]} == {
        "upper_arm.gcode", "lower_arm.gcode", "gripper.gcode"}
    expected = {"upper_arm.gcode": b"G1 UA v1\n", "lower_arm.gcode": b"G1 LA\n",
                "gripper.gcode": b"G1 GR\n"}
    assert {path: (drive / path).read_bytes() for path in expected} == expected
    snapshot = _tree(drive)
    retry = _publish(drive, "arm", jobs)
    assert retry["ok"] and not retry["changed"] and retry["revision"] == result["revision"]
    assert _tree(drive) == snapshot


def test_failed_legacy_migration_restores_files_sidecars_and_archives(drive, tmp_path, monkeypatch):
    _legacy_project(drive, stale=False)
    old_archive = drive / "CHUDWARE/ARCHIVE/ARM/ARM_RETIRED_BY_R001.zip"
    old_archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(old_archive, "w") as zf:
        zf.writestr("PRINT/ARM/R00101_UPPER_ARM.gcode", b"G1 archived upper\n")
    snapshot = _tree(drive)
    real = pm._move
    failed = False

    def unplugged(src, dst):
        nonlocal failed
        if dst == drive / "upper_arm.gcode" and not failed:
            failed = True
            raise OSError(errno.EIO, "simulated migration unplug")
        real(src, dst)

    monkeypatch.setattr(pm, "_move", unplugged)
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert not result["ok"] and result["error"] == "publish_rolled_back", result
    assert failed
    assert _tree(drive) == snapshot


@pytest.mark.parametrize("section", ["place", "retire", "metadata"])
def test_flat_recovery_cannot_mutate_another_projects_managed_file(drive, tmp_path, section):
    other = [_job(tmp_path / "other", "bracket", b"G1 other bracket\n")]
    assert _publish(drive, "other", other)["ok"]
    sha = hashlib.sha256(b"G1 other bracket\n").hexdigest()
    journal = {
        "schema": "chudware.print_media_txn/1", "txn": "0123456789abcdef",
        "phase": "swapping", "project": "ARM", "layout": "root", "revision": 1,
        "retire": [], "place": [], "metadata": []}
    if section == "place":
        journal[section] = [{"path": "bracket.gcode", "sha256": sha}]
    elif section == "retire":
        journal[section] = [{"path": "bracket.gcode", "sha256": sha,
                             "held": ".chudware_staging_0123456789abcdef/old-00.part"}]
    else:
        protected = drive / ".chudware_OTHER_manifest.json"
        journal[section] = [{"path": protected.name, "old": None,
                             "new_sha256": hashlib.sha256(protected.read_bytes()).hexdigest()}]
    (drive / ".chudware_JOURNAL.json").write_text(json.dumps(journal))
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert not result["ok"] and result["error"] == "journal_invalid", result
    assert _tree(drive) == snapshot


@pytest.mark.parametrize("name", [".chudware_ARM_manifest.json", ".chudware_ARM_README.txt"])
def test_hidden_metadata_collision_preserves_unknown_data(drive, tmp_path, name):
    (drive / name).write_bytes(b"unrelated data that must not be overwritten")
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert not result["ok"], result
    assert _tree(drive) == snapshot


@pytest.mark.parametrize("tamper_path", [
    None, "upper_arm.gcode", "lower_arm.gcode", "wrist.gcode",
    ".chudware_ARM_manifest.json", ".chudware_ARM_README.txt"])
def test_committed_publish_keeps_held_originals_until_new_files_and_metadata_verify(
        drive, tmp_path, monkeypatch, tamper_path):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    originals = {name: (drive / name).read_bytes()
                 for name in ("upper_arm.gcode", "lower_arm.gcode", "gripper.gcode")}
    jobs = _rev2(tmp_path)
    real = pm.os.unlink
    failed = False

    def unplugged(path, *args, **kwargs):
        nonlocal failed
        if Path(path).name.startswith("retired-"):
            failed = True
            raise OSError(errno.EIO, "simulated power loss before deleting held originals")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(pm.os, "unlink", unplugged)
    published = _publish(drive, "arm", jobs)
    assert published["ok"], published
    assert failed
    assert any(w["code"] == "cleanup_incomplete" for w in published["warnings"])
    monkeypatch.undo()
    pending = _inventory(drive)["interrupted_publish"]
    assert pending["valid"] and pending["phase"] == "committed"
    journal = json.loads((drive / ".chudware_JOURNAL.json").read_text())
    for entry in journal["retire"]:
        assert (drive / entry["held"]).read_bytes() == originals[entry["path"]]
    _assert_no_archives(drive)
    if tamper_path is not None:
        (drive / tamper_path).write_bytes(b"corrupt committed result")
        snapshot = _tree(drive)
        refused = _publish(drive, "arm", jobs)
        assert not refused["ok"] and refused["error"] == "recovery_failed", refused
        assert _tree(drive) == snapshot
    else:
        recovered = _publish(drive, "arm", jobs)
        assert recovered["ok"] and recovered["revision"] == 2, recovered
        assert recovered["recovered"]["action"] == "completed"
        _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v2\n", "lower_arm.gcode": b"G1 LA\n",
                                 "wrist.gcode": b"G1 WR\n",
                                 "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
        _assert_no_archives(drive)
        _assert_absent_bytes(drive, b"G1 UA v1\n", b"G1 GR\n")


@pytest.mark.parametrize("layout", ["root", "project"])
def test_successful_revision_leaves_no_deprecated_toolpath_bytes_anywhere(
        drive, tmp_path, layout):
    old = [_job(tmp_path / "old", "base", b"G1 DEPRECATED BASE\n"),
           _job(tmp_path / "old", "forearm_lower", b"G1 DEPRECATED FOREARM\n"),
           _job(tmp_path / "old", "discarded_part", b"G1 DEPRECATED PART\n")]
    assert _publish(drive, "robarm", old, layout=layout)["ok"]
    new = [_job(tmp_path / "new", "base", b"G1 CURRENT BASE\n"),
           _job(tmp_path / "new", "forearm_lower", b"G1 CURRENT FOREARM\n")]
    result = _publish(drive, "robarm", new, layout=layout,
                      retire_paths=["old_upper_arm.gcode"])
    assert result["ok"], result
    prefix = "" if layout == "root" else "ROBARM/"
    _assert_delivery(drive, {prefix + "base.gcode": b"G1 CURRENT BASE\n",
                             prefix + "forearm_lower.gcode": b"G1 CURRENT FOREARM\n"},
                     folders=set() if layout == "root" else {"ROBARM"})
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 DEPRECATED BASE\n", b"G1 DEPRECATED FOREARM\n",
                         b"G1 DEPRECATED PART\n", b"G1 legacy upper arm\n")


@pytest.mark.parametrize("failed_name", [
    "wrist.gcode", ".chudware_ARM_manifest.json", ".chudware_ARM_README.txt"])
def test_failed_new_file_readback_preserves_old_bytes_and_unrecognized_changes(
        drive, tmp_path, monkeypatch, failed_name):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    snapshot = _tree(drive)
    real = pm._move
    failed = False

    def corrupt_after_write(src, dst):
        nonlocal failed
        real(src, dst)
        if dst == drive / failed_name and not failed:
            failed = True
            dst.write_bytes(b"corrupt new file during readback")

    monkeypatch.setattr(pm, "_move", corrupt_after_write)
    result = _publish(drive, "arm", _rev2(tmp_path))
    assert not result["ok"] and result["error"] == "rollback_incomplete", result
    assert failed, "fault must corrupt an actual publication write"
    assert (drive / failed_name).read_bytes() == b"corrupt new file during readback"
    journal = json.loads((drive / ".chudware_JOURNAL.json").read_text())
    assert journal["phase"] == "swapping"
    for entry in journal["retire"]:
        original = drive / entry["path"]
        held = drive / entry["held"]
        assert ((original.is_file() and original.read_bytes() == snapshot[entry["path"]])
                or (held.is_file() and held.read_bytes() == snapshot[entry["path"]]))
    _assert_no_archives(drive)


@pytest.mark.parametrize("tamper_path", [
    None, "PRINT/ARM/R00201_UPPER_ARM.gcode", "PRINT/ARM/chudware_manifest.json"])
def test_legacy_committed_recovery_deletes_owned_archive_only_after_verification(
        drive, tmp_path, tamper_path):
    current = _legacy_project(drive, stale=False)
    txn = "0123456789abcdef"
    held = f"CHUDWARE/.staging/{txn}/old-00.part"
    retired_body = b"G1 legacy held original\n"
    retired_path = "PRINT/ARM/R00101_UPPER_ARM.gcode"
    (drive / held).parent.mkdir(parents=True)
    (drive / held).write_bytes(retired_body)
    archive_path = "CHUDWARE/ARCHIVE/ARM/ARM_RETIRED_BY_R002.zip"
    archive = drive / archive_path
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(retired_path, retired_body)
    metadata_paths = ["PRINT/ARM/chudware_manifest.json", "PRINT/ARM/README.txt"]
    journal = {
        "schema": "chudware.print_media_txn/1", "txn": txn, "phase": "committed",
        "project": "ARM", "revision": 2,
        "retire": [{"path": retired_path, "held": held,
                    "sha256": hashlib.sha256(retired_body).hexdigest()}],
        "place": [{"path": path, "sha256": hashlib.sha256(body).hexdigest()}
                  for path, body in current.items()],
        "archive": {"path": archive_path,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
        "metadata": [{"path": path, "old": None,
                      "new_sha256": hashlib.sha256((drive / path).read_bytes()).hexdigest()}
                     for path in metadata_paths],
    }
    (drive / "CHUDWARE/JOURNAL.json").write_text(json.dumps(journal))
    pending = _inventory(drive)["interrupted_publish"]
    assert pending["valid"] and pending["phase"] == "committed"
    if tamper_path is not None:
        (drive / tamper_path).write_bytes(b"corrupt legacy current result")
        snapshot = _tree(drive)
        result = _publish(drive, "arm", _rev1(tmp_path))
        assert not result["ok"] and result["error"] == "recovery_failed", result
        assert _tree(drive) == snapshot
        assert (drive / held).read_bytes() == retired_body
    else:
        result = _publish(drive, "arm", _rev1(tmp_path))
        assert result["ok"] and result["recovered"]["action"] == "completed", result
        _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v1\n", "lower_arm.gcode": b"G1 LA\n",
                                 "gripper.gcode": b"G1 GR\n",
                                 "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
        _assert_no_archives(drive)
        _assert_absent_bytes(drive, retired_body)


@pytest.mark.parametrize("archive_path", [
    "operator_backup.zip", ".chudware_OTHER_RETIRED_BY_R001.zip",
    "CHUDWARE/ARCHIVE/OTHER/OTHER_RETIRED_BY_R001.zip"])
@pytest.mark.parametrize("phase", ["swapping", "committed"])
def test_recovery_cannot_delete_unknown_or_other_project_archives(
        drive, tmp_path, archive_path, phase):
    archive = drive / archive_path
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("foreign.gcode", b"G1 protected archived toolpath\n")
    journal = {
        "schema": "chudware.print_media_txn/1", "txn": "0123456789abcdef",
        "phase": phase, "project": "ARM", "layout": "root", "revision": 1,
        "retire": [], "place": [], "metadata": [],
        "delete_archives": [{
            "path": archive_path, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "held": ".chudware_staging_0123456789abcdef/archive-00.part"}],
    }
    (drive / ".chudware_JOURNAL.json").write_text(json.dumps(journal))
    snapshot = _tree(drive)
    result = _publish(drive, "arm", _rev1(tmp_path))
    assert not result["ok"] and result["error"] == "journal_invalid", result
    assert _tree(drive) == snapshot


def test_old_flat_committed_journal_deletes_archive_and_relocated_archives(drive, tmp_path):
    assert _publish(drive, "arm", _rev1(tmp_path))["ok"]
    jobs = _rev2(tmp_path)
    published = _publish(drive, "arm", jobs)
    assert published["ok"], published
    archive_paths = [".chudware_ARM_RETIRED_BY_R001.zip",
                     ".chudware_ARM_RETIRED_BY_R002.zip"]
    for path in archive_paths:
        with zipfile.ZipFile(drive / path, "w") as zf:
            zf.writestr("old_upper_arm.gcode", b"G1 previously archived original\n")
    txn = "0123456789abcdef"
    held = f".chudware_staging_{txn}/old-00.part"
    (drive / held).parent.mkdir()
    (drive / held).write_bytes(b"G1 UA v1\n")
    metadata_paths = [".chudware_ARM_manifest.json", ".chudware_ARM_README.txt"]
    journal = {
        "schema": "chudware.print_media_txn/1", "txn": txn, "phase": "committed",
        "project": "ARM", "layout": "root", "revision": 2,
        "retire": [{"path": "upper_arm.gcode", "held": held,
                    "sha256": hashlib.sha256(b"G1 UA v1\n").hexdigest()}],
        "place": [{"path": entry["path"], "sha256": entry["sha256"]}
                  for entry in published["current"]],
        "archive": {"path": archive_paths[1],
                    "sha256": hashlib.sha256((drive / archive_paths[1]).read_bytes()).hexdigest()},
        "relocate": [{"path": "CHUDWARE/ARCHIVE/ARM/ARM_RETIRED_BY_R001.zip",
                      "dest": archive_paths[0],
                      "sha256": hashlib.sha256((drive / archive_paths[0]).read_bytes()).hexdigest()}],
        "metadata": [{"path": path, "old": None,
                      "new_sha256": hashlib.sha256((drive / path).read_bytes()).hexdigest()}
                     for path in metadata_paths],
    }
    (drive / ".chudware_JOURNAL.json").write_text(json.dumps(journal))
    pending = _inventory(drive)["interrupted_publish"]
    assert pending["valid"] and pending["phase"] == "committed"
    result = _publish(drive, "arm", jobs)
    assert result["ok"] and result["recovered"]["action"] == "completed", result
    assert result["revision"] == 2
    _assert_delivery(drive, {"upper_arm.gcode": b"G1 UA v2\n", "lower_arm.gcode": b"G1 LA\n",
                             "wrist.gcode": b"G1 WR\n",
                             "old_upper_arm.gcode": b"G1 legacy upper arm\n"})
    _assert_no_archives(drive)
    _assert_absent_bytes(drive, b"G1 UA v1\n", b"G1 previously archived original\n")
