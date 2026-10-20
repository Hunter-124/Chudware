"""NUM-4 consumer proof through real gateway, hostd, MCP tools and actor mounts.

Uses the explicit disposable quota helper/storage prerequisites of
_two_tenant_support. No namespace, session registry, export or archive read is
mocked. These local fixtures do not qualify production deployment authority.
The namespace, not a new /work-only destination policy, is the filesystem
boundary: /actor, private /tmp scratch and desktop-selected exports remain valid.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from _two_tenant_support import (
    A, B, C, denial, disposable_postgres, gateway_harness, real_payload, two_tenants,
)


NOT_FOUND = {"ok": False, "error": "not_found", "reason": "not_found", "code": "not_found"}


def _probe(ws, code, name):
    result = ws.ok("run_model", {"name": name, "code": code + "\nresult = Box(1, 1, 1)\n"}, timeout=90)
    rows = [line.removeprefix("ACCEPTANCE:") for line in result["stdout"].splitlines()
            if line.startswith("ACCEPTANCE:")]
    assert len(rows) == 1, result
    return json.loads(rows[0])


def _scan(marker):
    return ("ply\nformat ascii 1.0\n" + f"comment {marker}\n" +
            "element vertex 3\nproperty float x\nproperty float y\nproperty float z\n"
            "element face 1\nproperty list uchar int vertex_indices\nend_header\n"
            "0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n").encode()


def _archive(path, session_id, owner, mesh_name, mesh, foreign_marker):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("session.json"))
        assert manifest["session_id"] == session_id
        assert manifest["_owner"] == list(owner)
        assert archive.read("mesh/" + mesh_name) == mesh
        assert all(foreign_marker.encode() not in archive.read(name)
                   for name in archive.namelist())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root):
    """Compare actual host entries, including links and newly created parents."""
    entries = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries[relative] = ("symlink", str(path.readlink()))
        elif path.is_file():
            entries[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        elif path.is_dir():
            entries[relative] = ("directory",)
        else:
            raise AssertionError(("unexpected fixture entry", path))
    return entries


@pytest.mark.parametrize("workspace", ["personal", "shared-team"])
def test_num4_owned_bundle_and_foreign_destination_boundary(two_tenants, workspace):
    h = two_tenants
    team = h.team if workspace == "shared-team" else ""
    other = C if team else B
    a, foreign = h.socket(A, team), h.socket(other, team)
    pa, pf = h.principal(A, team), h.principal(other, team)
    if team:
        assert pa.cell_id == pf.cell_id and pa.actor_key != pf.actor_key
    else:
        assert pa.cell_id != pf.cell_id
    sessions, scans = {}, {}
    for email, ws, marker in ((A, a, "NUM4-A-private-mesh"),
                              (other, foreign, "NUM4-foreign-private-mesh")):
        sessions[email] = ws.ok("reveng_start_session", {"name": marker})["session_id"]
        source = f"num4-{'a' if email == A else 'foreign'}-scan.ply"
        scans[email] = (source, _scan(marker))
        report = _probe(ws, f'''
import json
from pathlib import Path
source = Path("/work") / {source!r}
source.write_bytes({scans[email][1]!r})
print("ACCEPTANCE:" + json.dumps({{"bytes": source.stat().st_size}}))
''', "num4-source-" + ("a" if email == A else "foreign"))
        assert report["bytes"] == len(scans[email][1])
        imported = ws.ok("import_scan_mesh", {"session_id": sessions[email], "path": source})
        assert imported["session"]["session_id"] == sessions[email]
    own, theirs = sessions[A], sessions[other]
    assert own != theirs and own.startswith("re_") and theirs.startswith("re_")
    owner = (pa.workspace_key, pa.actor_key)
    mesh_name, mesh = scans[A]
    foreign_marker = "NUM4-foreign-private-mesh"

    # Every admitted destination writes an actual CRC-valid archive, not an echo.
    selected = pa.root / "num4-a-deliveries"
    selected.mkdir()
    (pa.root / "num4-own-link").symlink_to(selected.name, target_is_directory=True)
    actor_hex = hashlib.sha256(pa.actor_key.encode()).hexdigest()[:32]
    actor_root = h.running[pa.cell_id].directory / "state/actors" / actor_hex
    destinations = (
        ("num4-a-deliveries/relative.zip", selected / "relative.zip"),
        ("/work/num4-a-deliveries/absolute.zip", selected / "absolute.zip"),
        ("num4-own-link/linked.zip", selected / "linked.zip"),
        ("/actor/home/num4-deliveries/private.zip", actor_root / "home/num4-deliveries/private.zip"),
        ("~/num4-deliveries/home.zip", actor_root / "home/num4-deliveries/home.zip"),
    )
    hashes = []
    for destination, host_path in destinations:
        result = a.ok("export_reveng_bundle", {"session_id": own, "dest": destination})
        assert result["ok"] is True and isinstance(result["path"], str), result
        hashes.append(_archive(host_path, own, owner, mesh_name, mesh, foreign_marker))

    sentinel = pa.root / "num4-rejected.zip"
    sentinel.write_bytes(b"existing destination must survive")
    missing = pa.root / "num4-never-created" / "nested" / "rejected.zip"
    for handle in (theirs, "absent-session", "../escape", "/etc/passwd", "", None, False, 7, [], {}):
        for destination in ("num4-rejected.zip", "num4-never-created/nested/rejected.zip"):
            denial(a.request("export_reveng_bundle", {"session_id": handle, "dest": destination}), "not_found")
            assert sentinel.read_bytes() == b"existing destination must survive"
            assert not missing.parent.parent.exists()
    denial(a.request("export_reveng_bundle", {"dest": "num4-rejected.zip"}), "not_found")
    assert sentinel.read_bytes() == b"existing destination must survive"

    # Exact foreign/absent/malformed envelopes at both actual Python consumers,
    # plus /tmp export/readback within the lifetime of the genuine nested actor.
    report = _probe(a, f'''
import hashlib, json, os, zipfile
from pathlib import Path
from services.agent import request_context
from services.reveng import worker_rpc
assert os.environ["CHUDWARE_ACTOR_SANDBOX"] == "1"
assert request_context.owner_key() == {owner!r}
expected = {NOT_FOUND!r}
export = worker_rpc._tool_fn("export_reveng_bundle")
handler = worker_rpc.REVENG_WORKER_METHODS["export_reveng_bundle"]
existing = Path("/tmp/num4-existing.zip")
existing.write_bytes(b"untouched scratch sentinel")
missing = Path("/tmp/num4-missing/nested/rejected.zip")
refusals = 0
for handle in ({theirs!r}, "absent-session", "../escape", "/etc/passwd", ".", "..", "", "evil\\x00handle", None, False, 7, [], {{}}):
    for destination in (existing, missing):
        supplied = {{"session_id": handle, "dest": str(destination)}}
        direct = json.loads(export(**supplied))
        bridged = handler(supplied, lambda _event: None)
        assert direct == bridged == expected, (handle, direct, bridged)
        assert existing.read_bytes() == b"untouched scratch sentinel"
        assert not missing.parent.parent.exists()
        refusals += 1
scratch = Path("/tmp/num4-private-delivery/owned.zip")
written = json.loads(export(session_id={own!r}, dest=str(scratch)))
assert written == {{"ok": True, "path": str(scratch)}}, written
with zipfile.ZipFile(scratch) as archive:
    assert archive.testzip() is None
    manifest = json.loads(archive.read("session.json"))
    assert manifest["session_id"] == {own!r} and manifest["_owner"] == list({owner!r})
    assert archive.read("mesh/" + {mesh_name!r}) == {mesh!r}
    assert all({foreign_marker.encode()!r} not in archive.read(name) for name in archive.namelist())
print("ACCEPTANCE:" + json.dumps({{"refusals": refusals, "scratch_sha256": hashlib.sha256(scratch.read_bytes()).hexdigest()}}))
''', "num4-real-consumers")
    assert report["refusals"] == 26 and len(report["scratch_sha256"]) == 64

    # Host/sibling-workspace/foreign-actor locations are real outside sentinels.
    # A spelling may name private tmpfs storage in the actor and succeed there;
    # it must never overwrite or create a parent in the actual outside location.
    outside = h.root / "num4-host-outside"
    outside.mkdir()
    (outside / "sentinel.zip").write_bytes(b"host sentinel")
    sibling = h.principal(B).root
    (sibling / "num4-sibling-sentinel.zip").write_bytes(b"sibling workspace sentinel")
    foreign_hex = hashlib.sha256(pf.actor_key.encode()).hexdigest()[:32]
    foreign_private = h.running[pf.cell_id].directory / "state/actors" / foreign_hex / "home/num4-boundary"
    foreign_private.mkdir()
    (foreign_private / "sentinel.zip").write_bytes(b"foreign actor sentinel")
    for name, target in (("num4-host-link", outside), ("num4-sibling-link", sibling),
                         ("num4-foreign-actor-link", foreign_private)):
        (pa.root / name).symlink_to(target, target_is_directory=True)
    (pa.root / "num4-outside-file-link.zip").symlink_to(outside / "sentinel.zip")
    roots = (outside, sibling, foreign_private)
    before = {str(root): _snapshot(root) for root in roots}
    attempts = (
        str(outside / "sentinel.zip"),
        str(outside / "not-created/nested/bundle.zip"),
        "/work/../../" + str(sibling / "num4-sibling-sentinel.zip").lstrip("/"),
        "/work/../../" + str(foreign_private / "not-created/bundle.zip").lstrip("/"),
        "num4-host-link/sentinel.zip",
        "num4-sibling-link/not-created/bundle.zip",
        "num4-foreign-actor-link/sentinel.zip",
        "num4-outside-file-link.zip",
        f"/state/actors/{foreign_hex}/home/num4-boundary/sentinel.zip",
    )
    for destination in attempts:
        response = a.request("export_reveng_bundle", {"session_id": own, "dest": destination})
        assert "error" in response or isinstance(response.get("result", {}).get("ok"), bool), response
        assert before == {str(root): _snapshot(root) for root in roots}, (destination, response)
    assert all(len(value) == 64 for value in hashes)
    print(f"SCENARIO NUM-4/{workspace}: real owned archive CRC/manifest/mesh readback; "
          "foreign/absent/malformed consumers identical before mutation; "
          "workspace/actor/home/symlink/private-tmp exports work; host/sibling/foreign actor unchanged")
