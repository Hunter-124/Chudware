"""Real two-tenant acceptance; no daemon, actor, auth, membership or database mocks.

Run with the gateway dependency interpreter inside an operator-prepared private
loop-image XFS/ext4 project-quota mount namespace. Set
CHUDWARE_TEST_CELL_STORAGE_ROOT to its private service-owned fixture parent and
CHUDWARE_TEST_CELL_QUOTA_HELPER to the explicit root quota-helper Unix socket.
The helper provisions real empty workspace/state/bucket project roots and stages
the known development venv read-only; no test invokes sudo or a product unit.
Bubblewrap/user namespaces and prebuilt native artifacts remain prerequisites,
not silently emulated fallbacks. Production staging still requires the reviewed
authenticated runtime lock. Synthetic provider keys and official-wire SSE stay local.

Each test names an audit acceptance scenario. Metadata fixtures below are created
by real run_model code inside the actor, using the production registries. They
are not claimed to be running agents. The real AI/control leg is separately
skipped with the exact offline pin-verification failure when chud is unavailable.
The resource leg uses one transient, auto-collected user scope containing only
its disposable cell. Real pinned provider calls go through the real gateway broker
to a local official-wire fixture, not a live LLM or provider billing/cache proof.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from _two_tenant_support import (
    A, B, C, AMBIENT, BYOK, REPO, box, denial, quota_plan,
    disposable_postgres, gateway_harness, real_payload, two_tenants,
)
from _provider_wire_support import TEXT, THINKING, SIGNATURE


def policy():
    return json.loads((REPO / "contracts/cell_methods.json").read_text())


def probe(ws, code, name="actor-probe", timeout=90):
    """Execute unchanged run_model + nested real actor sandbox, with real geometry."""
    result = ws.ok("run_model", {"name": name, "code": code + "\nresult = Box(1, 1, 1)\n"}, timeout=timeout)
    rows = [line.removeprefix("ACCEPTANCE:") for line in result["stdout"].splitlines()
            if line.startswith("ACCEPTANCE:")]
    assert rows, result
    return json.loads(rows[-1])


def seed_private_records(ws, marker):
    return probe(ws, f'''
import json
from services.agent.subagent_registry import register_session
from services.dag.storage import create_run
record = create_run("same-run")
record.db.touch()
record.graph.write_text(json.dumps({{"name": {marker!r}, "nodes": [{{"id": "n1"}}]}}))
child = register_session("same-child", role="engineer", brief={marker!r}, status="pending")
print("ACCEPTANCE:" + json.dumps({{"dag_id": record.run_id, "child": child["id"]}}))
''', "records")


def assert_denied(response):
    assert "error" in response or response.get("result", {}).get("ok") is False, response


def assert_stopped(response):
    result = response.get("result", {})
    assert (result.get("stop_reason") in {"cancelled", "manager_stop"} or
            result.get("turn", {}).get("cancelled") is True), response
    return result


def assert_no_values(value, forbidden):
    text = json.dumps(value, sort_keys=True)
    assert all(item not in text for item in forbidden), text


def start_provider_turn(h, ws, handles, email, *, provider="openai", key=None, request="Answer READY; do not use tools."):
    wire = h.provider_wire
    key = key or BYOK[email]
    offset = len(wire.records)
    wire.hold(key)
    ident = ws.send("agent_admin_turn", {**handles, "provider": provider,
        "model": "claude-sonnet-4-5" if provider == "anthropic" else "gpt-4o-mini",
        "request": request, "workspace_dir": "/work"})
    row = wire.wait_request(key, offset=offset, primary=True)
    assert row.first_delta.wait(15), "official-wire fixture did not send a first delta"
    ws.wait_event(lambda event: event.get("data", {}).get("run_id") == handles["run_id"]
                  and TEXT.strip() in json.dumps(event), timeout=30)
    assert not row.completed and ident not in ws.replies, "provider response buffered until terminal"
    return ident, row


_KEY_SCAN_CHUNK_BYTES = 1024 * 1024
_KEY_SCAN_CELL_BYTES = 8 * 1024 * 1024 * 1024


def assert_stream_has_no_keys(stream, forbidden, *, byte_limit, label):
    """Read every byte, retaining only the longest possible split-key suffix."""
    overlap = max(map(len, forbidden)) - 1
    suffix = b""
    scanned = 0
    while True:
        chunk = stream.read(min(_KEY_SCAN_CHUNK_BYTES, byte_limit - scanned + 1))
        if not chunk:
            return scanned
        scanned += len(chunk)
        assert scanned <= byte_limit, "cell artifact inspection exceeded aggregate byte bound"
        data = suffix + chunk
        assert not any(value in data for value in forbidden), f"upstream key in {label}"
        suffix = data[-overlap:] if overlap else b""


def assert_no_upstream_keys(h, *additional):
    """Inspect only owned disposable cell artifacts/process descendants, bounded."""
    forbidden = tuple(value.encode() for value in (AMBIENT, *BYOK.values(), *additional))
    for cell in h.running.values():
        inspected = scanned = 0
        for root in (cell.directory / "state", cell.principal.root):
            for path in root.rglob("*"):
                inspected += 1
                assert inspected < 10000, "cell artifact inspection exceeded fixture bound"
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    scanned += assert_stream_has_no_keys(
                        stream, forbidden, byte_limit=_KEY_SCAN_CELL_BYTES - scanned,
                        label=f"cell artifact {path.name}")
        pending = [cell.proc.pid]
        seen = set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            assert len(seen) <= 512, "process inspection exceeded fixture bound"
            try:
                proc = Path("/proc") / str(pid)
                for task in (proc / "task").iterdir():
                    try:
                        pending.extend(int(value) for value in (task / "children").read_text().split())
                    except FileNotFoundError:
                        continue
                for name in ("environ", "cmdline"):
                    with (proc / name).open("rb") as stream:
                        data = stream.read(1024 * 1024 + 1)
                    assert len(data) <= 1024 * 1024, "oversized owned process metadata"
                    # The outer scratch launcher intentionally contains the
                    # ambient sentinel. It must disappear at the cell boundary.
                    prohibited = forbidden[1:] if pid == cell.proc.pid else forbidden
                    assert not any(value in data for value in prohibited), f"upstream key in owned process {name}"
            except (FileNotFoundError, ProcessLookupError):
                pass
        assert not any(target.startswith(("api.openai.com:", "api.anthropic.com:", "api.x.ai:",
                                        "openrouter.ai:", "generativelanguage.googleapis.com:"))
                       for target in cell.broker.targets), "provider traffic reached CONNECT lane"


@pytest.mark.parametrize("offset", [
    0, _KEY_SCAN_CHUNK_BYTES - 1, _KEY_SCAN_CHUNK_BYTES - 17,
    17 * _KEY_SCAN_CHUNK_BYTES + 23,
])
def test_upstream_key_scanner_detects_boundaries_and_large_elf_tail(tmp_path, offset):
    """Size, ELF signatures and a key ending at EOF never exempt artifact bytes."""
    artifact = tmp_path / "extracted-native-elf"
    secret = BYOK[A].encode()
    with artifact.open("wb") as stream:
        stream.write(b"\x7fELF")
        stream.seek(offset)
        stream.write(secret)
    with artifact.open("rb") as stream, pytest.raises(AssertionError, match="upstream key"):
        assert_stream_has_no_keys(stream, (secret,), byte_limit=_KEY_SCAN_CELL_BYTES,
                                  label="scanner regression artifact")


def test_upstream_key_scanner_reads_complete_large_elf_and_refuses_excess_work(tmp_path):
    artifact = tmp_path / "extracted-native-elf"
    size = 17 * _KEY_SCAN_CHUNK_BYTES + 23
    with artifact.open("wb") as stream:
        stream.write(b"\x7fELF")
        stream.seek(size - 1)
        stream.write(b"\x00")
    with artifact.open("rb") as stream:
        assert assert_stream_has_no_keys(
            stream, (BYOK[A].encode(),), byte_limit=size, label="large clean ELF") == size
    with artifact.open("rb") as stream, pytest.raises(AssertionError, match="aggregate byte bound"):
        assert_stream_has_no_keys(stream, (BYOK[A].encode(),), byte_limit=size - 1,
                                  label="bounded large ELF")


def test_every_registered_method_has_explicit_cell_classification():
    """Inventory covers native, Python, delegated and daemon lifecycle registrations."""
    specification = importlib.util.spec_from_file_location("acceptance_cell_methods", REPO / "scripts/cell_methods.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    entries = module.load_policy()
    module.validate_policy(entries)
    assert set(entries) == module.harvest_registered_methods()
    print(f"SCENARIO inventory: {len(entries)} explicitly classified registered methods")


def test_real_gateway_same_name_paths_membership_and_private_chats(gateway_harness):
    """Actual HTTP admission + Postgres; this leg does not require a native binary."""
    h = gateway_harness
    pa, pb = h.principal(A), h.principal(B)
    for principal, text in ((pa, "A-private-file"), (pb, "B-private-file")):
        principal.root.mkdir(parents=True, exist_ok=True)
        (principal.root / "same.txt").write_text(text)
    for email, marker, foreign in ((A, "A-private-file", "B-private-file"), (B, "B-private-file", "A-private-file")):
        listing = h.client.get("/workspace/list", headers={"x-dev-user": email})
        assert listing.status_code == 200, listing.text
        assert listing.json()["workspace_key"] == h.principal(email).workspace_key
        assert "same.txt" in json.dumps(listing.json())
        store = {"version": 2, "active": "same-chat", "sessions": [{"id": "same-chat", "marker": marker}]}
        saved = h.client.put("/workspace/chat", headers={"x-dev-user": email}, json=store)
        assert saved.status_code == 200, saved.text
        loaded = h.client.get("/workspace/chat", headers={"x-dev-user": email})
        assert loaded.status_code == 200, loaded.text
        assert_no_values(loaded.json(), [foreign])
    for bad in (str(pb.root), "../" + pb.root.name, "nul\x00path"):
        response = h.client.post("/workspace/mkdir", headers={"x-dev-user": A}, json={"path": bad})
        assert response.status_code in (400, 403), response.text
    response = h.client.post(f"/workspace/mkdir?team={h.team}", headers={"x-dev-user": B}, json={"path": "viewer-write"})
    assert response.status_code == 403, response.text
    assert (pb.root / "same.txt").read_text() == "B-private-file"
    print("SCENARIO gateway HTTP: same-name roots, private chats, unsafe paths, viewer mutation refused")


def test_real_gateway_rate_quota_is_per_actor_without_cross_tenant_charge(gateway_harness, monkeypatch):
    """One actor's personal/team abuse budget never consumes another actor's."""
    h = gateway_harness
    monkeypatch.setattr(h.gateway.limits.api_writes, "limit", 1)
    outcomes = []
    for email, team, directory in ((A, "", "a-personal"), (A, h.team, "a-team"), (B, "", "b-personal")):
        response = h.client.post("/workspace/mkdir" + (f"?team={team}" if team else ""),
                                 headers={"x-dev-user": email}, json={"path": directory})
        outcomes.append((email, team or "personal", response.status_code, response.text))
    assert [item[2] for item in outcomes] == [200, 429, 200], outcomes
    exhausted = h.client.post("/workspace/mkdir", headers={"x-dev-user": A}, json={"path": "a-second"})
    assert exhausted.status_code == 429, exhausted.text
    print("SCENARIO quota/rate: actor abuse budget spans personal/team; B budget unaffected by A")


def test_same_name_and_foreign_paths_symlinks_assembly_references(two_tenants):
    h = two_tenants
    a, b = h.socket(A), h.socket(B)
    aid, ageometry = box(a, length=10)
    bid, bgeometry = box(b, length=20)
    assert ageometry["volume"] != bgeometry["volume"]
    # Native doc IDs are workspace-scoped: equal counters may only resolve locally.
    if aid == bid:
        assert a.ok("doc_activate", {"doc_id": bid})["volume"] == ageometry["volume"]
        assert b.ok("doc_activate", {"doc_id": aid})["volume"] == bgeometry["volume"]
    for ws, ident in ((a, aid), (b, bid)):
        saved = ws.ok("doc_save", {"doc_id": ident, "path": "same.step+"})
        assert saved["cache"]["written"] is True, saved
    pa, pb = h.principal(A), h.principal(B)
    foreign_bytes = (pb.root / "same.step+").read_bytes()
    foreign_marker = pb.root / "B-only-secret.txt"
    foreign_marker.write_text("B-only-secret-never-readable")
    (pa.root / "foreign-link").symlink_to(pb.root, target_is_directory=True)
    failures = []
    for bad in (str(pb.root / "same.step+"), "../" + pb.root.name + "/same.step+",
                "/work/../../" + str(pb.root).lstrip("/") + "/same.step+", "foreign-link/same.step+"):
        for method, params in (("doc_open", {"path": bad}),
                               ("doc_save", {"doc_id": aid, "path": bad}),
                               ("doc_apply_ops", {"doc_id": aid, "ops": [{"op": "add_feature", "feature": {
                                "type": "import", "name": "foreign", "params": {"source_path": bad}}}]})):
            response = a.request(method, params)
            try:
                if method == "doc_apply_ops":
                    assert_denied(response)
                    assert "foreign" not in json.dumps(a.ok("doc_tree", {"doc_id": aid}))
                else:
                    denial(response, "cell_refused")
            except AssertionError:
                failures.append((method, bad, response))
    for bad in ("same.step+\x00", "same.step+\x01", "same.step+\x7f"):
        a.send("doc_open", {"path": bad})
        denial(a.response(None), -32700)
    assembly, _ = box(a, "own-assembly", kind="assembly")
    inserted = a.ok("doc_apply_ops", {"doc_id": assembly, "ops": [
        {"op": "asm_insert_component", "source": {"path": "same.step+"}}]})
    assert inserted["rebuild"]["converged"] is True, inserted
    for bad in (str(pb.root / "same.step+"), "foreign-link/same.step+"):
        assert_denied(a.request("doc_apply_ops", {"doc_id": assembly, "ops": [
            {"op": "asm_insert_component", "source": {"path": bad}}]}))
        assert bad not in json.dumps(a.ok("doc_tree", {"doc_id": assembly})), "refused component was retained"
    assert (pb.root / "same.step+").read_bytes() == foreign_bytes
    assert foreign_marker.read_text() == "B-only-secret-never-readable"
    assert not failures, failures
    print("SCENARIO paths: same name independent; absolute/traversal/NUL/symlink/import/assembly cross-root refused")


def test_list_apis_and_guessed_document_session_run_subagent_job_ids(two_tenants):
    h = two_tenants
    a, b = h.socket(A), h.socket(B)
    aid, _ = box(a, "A-visible-only")
    retired = b.ok("doc_new", {"name": "B-retired-id"})["doc_id"]
    b.ok("doc_close", {"doc_id": retired})
    bid, _ = box(b, "B-visible-only")
    handles = {A: a.ok("agent_handles_new"), B: b.ok("agent_handles_new")}
    records = {A: seed_private_records(a, "A-record-marker"), B: seed_private_records(b, "B-record-marker")}
    assert records[A]["child"] != records[B]["child"]
    assert all(record["child"] != "same-child" for record in records.values())
    sessions = {A: a.ok("reveng_start_session", {"name": "A-reveng-only"}),
                B: b.ok("reveng_start_session", {"name": "B-reveng-only"})}
    for ws, email, other, own_doc, foreign_doc in ((a, A, B, aid, bid), (b, B, A, bid, aid)):
        documents = ws.ok("doc_list")
        assert own_doc in json.dumps(documents), documents
        forbidden = [f"{other.split('@')[0][-1].upper()}-visible-only"]
        if foreign_doc != own_doc:
            forbidden.append(foreign_doc)
        assert_no_values(documents, forbidden)
        for method, params in (("subagent_list", {}), ("list_reveng_sessions", {}),
                               ("dag_status", {"run_id": records[email]["dag_id"]})):
            listed = ws.ok(method, params)
            assert_no_values(listed, [f"{other.split('@')[0][-1].upper()}-record-marker",
                                     sessions[other]["session_id"], handles[other]["session_id"],
                                     records[other]["dag_id"]])
        for method in ("agent_list", "recovery_list"):
            denial(ws.request(method), "cell_refused")
        # Native document counters belong to the cell, not the actor. Foreign
        # counters must behave exactly like absent counters in this cell.
        assert foreign_doc != own_doc
        for method in ("doc_tree", "doc_snapshot", "attach_doc", "doc_close"):
            foreign_response = ws.request(method, {"doc_id": foreign_doc})
            absent_response = ws.request(method, {"doc_id": "doc-never-existed"})
            def absent_shape(response, ident):
                return json.dumps({key: value for key, value in response.items() if key != "id"},
                                  sort_keys=True).replace(ident, "<absent>")
            assert absent_shape(foreign_response, foreign_doc) == absent_shape(absent_response, "doc-never-existed"), (
                method, foreign_response, absent_response)
            assert_no_values(foreign_response, [forbidden[0]])
            if method == "doc_tree":
                denial(foreign_response, "unknown_doc")
        for guessed in (sessions[other]["session_id"], "missing-session"):
            denial(ws.request("get_reveng_session", {"session_id": guessed}), "not_found")
        for guessed in (records[other]["dag_id"], "missing-run", "../escape", "/tmp/escape"):
            response = ws.request("dag_status", {"run_id": guessed})
            denial(response, "not_found")
        for guessed in (records[other]["child"], "missing-child"):
            for method, extra in (("subagent_transcript", {}), ("subagent_terminate", {"confirm": True}),
                                  ("subagent_steer", {"text": "foreign steer"}), ("subagent_control", {"op": "pause"})):
                denial(ws.request(method, {"subagent_id": guessed, **extra}), "not_found")
        denial(ws.request("sim_cancel", {"job_id": "foreign-job-never-existed"}), "not_found")
    assert handles[A] != handles[B]
    print("SCENARIO objects: real doc/subagent/reveng/DAG lists and foreign/missing/path-shaped guessed IDs isolated")


def test_cross_tenant_cancel_steer_answer_close_release_handoff_refused(two_tenants):
    h = two_tenants
    a, b = h.socket(A), h.socket(B)
    foreign = a.ok("agent_handles_new")
    b.ok("agent_handles_new")
    for method, field, extra in (("agent_cancel", "run_id", {"provider": "openai"}),
                                 ("agent_steer", "run_id", {"provider": "openai", "text": "foreign steering"}),
                                 ("agent_answer", "run_id", {"provider": "openai", "text": "foreign answer"}),
                                 ("chudcode_close_session", "session_id", {"provider": "openai"}),
                                 ("chudcode_session_stats", "session_id", {}),
                                 ("session_handoff", "session_id", {"provider": "openai", "messages": []})):
        for guessed in (foreign[field], "missing-handle"):
            denial(b.request(method, {field: guessed, **extra}), "not_found")
    for guessed in (foreign["session_id"], "missing-handle"):
        denial(b.request("agent_handles_new", {"session_id": guessed}), "not_found")
    # Empty-run cancellation travels the actual native control path, not a worker mock.
    canceled = b.ok("agent_cancel", {"provider": "openai", "run_id": ""})
    assert canceled.get("run_ids", []) == [], canceled
    assert a.ok("agent_handles_new", {"session_id": foreign["session_id"]})["session_id"] == foreign["session_id"]
    # Private status/release have no public worker method. Invoke their actual
    # production owner gate from a real B actor; never stand up a fake host.
    guards = probe(b, f'''
import json
from services.agent.worker_rpc import chudcode_control
print("ACCEPTANCE:" + json.dumps({{
    action: chudcode_control(action, {{"session_id": {foreign["session_id"]!r}}})
    for action in ("status", "release")
}}))
''', "private-control-guards")
    for response in guards.values():
        assert response == {"ok": False, "reason": "not_found", "code": "not_found"}, response
    print("SCENARIO control: foreign cancel/steer/answer/close/stats/handoff refused; empty cancel own-only")


def test_viewer_full_manifest_denial_and_manager_stop_only(two_tenants):
    h = two_tenants
    editor, viewer, manager = h.socket(A, h.team), h.socket(B, h.team), h.socket(C, h.team)
    ident, _ = box(editor, "shared-readable")
    attached = viewer.ok("attach_doc", {"doc_id": ident, "mode": "rw"})
    assert attached["mode"] == "ro", attached
    assert viewer.ok("doc_tree", {"doc_id": ident})
    checked = editor_denials = manager_denials = 0
    for method, entry in policy().items():
        if entry["cell"] == "refuse" or entry["right"] != "read":
            code = "cell_refused" if entry["cell"] == "refuse" else "forbidden_right"
            denial(viewer.request(method, {"doc_id": ident}), code)
            checked += 1
        if entry["cell"] == "refuse" or entry["right"] == "manage":
            code = "cell_refused" if entry["cell"] == "refuse" else "forbidden_right"
            denial(editor.request(method, {"doc_id": ident}), code)
            editor_denials += 1
        if entry["cell"] == "refuse":
            denial(manager.request(method, {"doc_id": ident}), "cell_refused")
            manager_denials += 1
    # Every admitted manage method is an in-process profiler operation. Exercise
    # them only in this owned scratch cell; never issue hardware or device jobs.
    management = {method for method, entry in policy().items()
                  if entry["cell"] == "allow" and entry["right"] == "manage"}
    exercised = set()
    assert manager.ok("prof_status")["active"] is False
    try:
        manager.ok("prof_start", {"interval_us": 100000, "mode": "all"})
        exercised.add("prof_start")
        assert manager.ok("prof_status")["active"] is True
        for method in ("prof_target_push", "prof_target_pop", "prof_contention",
                       "prof_phases", "prof_phases_tree", "prof_report", "prof_tree"):
            response = manager.request(method)
            # An empty profiler may report no samples inside its result. The
            # permission boundary must admit it, not return an RPC refusal.
            assert "result" in response and "error" not in response, (method, response)
            exercised.add(method)
    finally:
        stopped_profile = manager.request("prof_stop")
        assert "result" in stopped_profile and "error" not in stopped_profile, stopped_profile
        exercised.add("prof_stop")
    assert manager.ok("prof_status")["active"] is False
    assert exercised == management, ("unexercised manage-right method", management - exercised)
    session = editor.ok("reveng_start_session", {"name": "editor-private-reveng"})["session_id"]
    for method in ("get_reveng_session", "reveng_splat_status", "reveng_finish_splat", "reveng_resolve_gate"):
        denial(manager.request(method, {"session_id": session}), "not_found")
    stopped = manager.request("reveng_cancel_splat", {"session_id": session})["result"]
    assert stopped["reason"] == "no_splat_job", stopped
    # This is an idle fixture. A real active manager cancellation is exercised
    # by the pinned-AI scenario, without granting any private-session reads.
    print(f"SCENARIO rights: viewer {checked}, editor {editor_denials}, manager {manager_denials} denied entries; all manage admissions; foreign stop-only")


def test_silent_observer_foreign_events_revocation_and_stale_mesh(two_tenants):
    h = two_tenants
    personal, silent = h.socket(A), h.socket(B)
    silent.ok("subscribe", {"doc_ids": "*"})
    foreign_doc, _ = box(personal, "foreign-event-marker")
    personal.ok("edit_hold", {"doc_id": foreign_doc})
    personal.ok("edit_release", {"doc_id": foreign_doc})
    silent.ok("ping")
    time.sleep(1.2)
    assert_no_values(silent.events, [foreign_doc, "foreign-event-marker", h.principal(A).actor_key])
    assert not any(event.get("event", "").startswith(("agent.", "mfg.", "daemon.", "child.")) for event in silent.events), silent.events
    editor, observer = h.socket(A, h.team), h.socket(B, h.team)
    ident, _ = box(editor, "shared-mesh")
    observer.ok("attach_doc", {"doc_id": ident, "mode": "rw"})
    snapshot = observer.ok("doc_snapshot", {"doc_id": ident})
    assert "aicm-cell:" in json.dumps(snapshot), snapshot
    url = "/files/aicm"
    query = {"workspace_key": h.principal(B, h.team).workspace_key, "doc_id": ident}
    fetched = h.client.get(url, params=query, headers={"x-dev-user": B})
    assert fetched.status_code == 200 and fetched.content, fetched.text
    before = len(observer.events)
    h.remove(B)
    observer.wait_closed()
    stale = h.client.get(url, params=query, headers={"x-dev-user": B})
    assert stale.status_code == 403, stale.text
    editor.ok("doc_set_name", {"doc_id": ident, "name": "after-revocation"})
    time.sleep(0.2)
    assert len(observer.events) == before, observer.events[before:]
    silent.ok("ping")  # B's independent personal workspace survives team removal.
    print("SCENARIO observer: no foreign events; silent removal closes 4403; stale mesh 403; personal cell intact")


def test_revocation_stops_actual_actor_execution(two_tenants):
    """A previously admitted editor execution is stopped, including its process tree."""
    h = two_tenants
    runner, observer = h.socket(A, h.team), h.socket(B, h.team)
    heartbeat = h.principal(A, h.team).root / "actor-heartbeat"
    request = runner.send("run_model", {"name": "revoked-execution", "code": '''
import time
from pathlib import Path
heartbeat = Path("/work/actor-heartbeat")
for tick in range(1200):
    heartbeat.write_text(str(tick))
    time.sleep(0.05)
result = Box(1,1,1)
'''})
    deadline = time.monotonic() + 45
    while not heartbeat.exists() and request not in runner.replies and time.monotonic() < deadline:
        time.sleep(0.05)
    assert heartbeat.exists(), (runner.replies.get(request), runner.diagnostics())
    old = h.principal(A, h.team)
    h.remove(A)
    runner.wait_closed()
    # Production stop grants 10 seconds of cooperative grace and 2 seconds
    # before SIGKILL. Permit that bounded grace, not an artificial instant stop.
    deadline = time.monotonic() + 20
    stopped_at = heartbeat.read_text()
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        current = heartbeat.read_text()
        if current != stopped_at:
            stopped_at, stable_since = current, time.monotonic()
        if time.monotonic() - stable_since >= 1:
            break
        time.sleep(0.05)
    assert time.monotonic() - stable_since >= 1, "revoked actor exceeded documented stop grace"
    time.sleep(0.3)
    assert heartbeat.read_text() == stopped_at, "revoked actor process tree resumed writing"
    denial(h.raw(old, "ping"), "revoked")
    observer.ok("ping")
    assert request > 0
    print("SCENARIO revocation: active real actor process tree stopped; stale trusted principal rejected; viewer survives")


def test_actor_sandboxes_ambient_credentials_files_proc_shell_predicate_and_egress(two_tenants):
    h = two_tenants
    sockets = {A: h.socket(A), B: h.socket(B)}
    for email, ws in sockets.items():
        other = B if email == A else A
        foreign = h.principal(other)
        forbidden_paths = [str(h.ambient_file), str(foreign.root), str(h.gateway.settings.cells_root),
                           "/state", "/run/gateway.sock", "/proc/1/root" + str(h.ambient_file)]
        report = probe(ws, f'''
import json, os, subprocess
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from services.mcp.fs_tools import register_shell_tools
from services.dag.predicate import evaluate
forbidden = {forbidden_paths!r}
visible = {{path: Path(path).exists() for path in forbidden}}
proc_leaks = []
for entry in Path("/proc").iterdir():
    if entry.name.isdigit():
        try:
            raw = (entry / "environ").read_bytes()
            if {AMBIENT!r}.encode() in raw or {BYOK[other]!r}.encode() in raw:
                proc_leaks.append(entry.name)
        except (OSError, PermissionError):
            pass
mcp = FastMCP("acceptance-shell")
register_shell_tools(mcp)
shell = mcp._tool_manager.get_tool("run_shell").fn(command="printf 'actor-shell-ok'; test ! -e /state; test ! -e " + {shlex.quote(str(h.ambient_file))!r}, cwd="/work", timeout_s=10)
rejected = []
for expression in ["__import__('os').system('id')", "x.__class__", "open('/etc/passwd')"]:
    try:
        evaluate(expression, {{"x": 1}})
        rejected.append(False)
    except ValueError:
        rejected.append(True)
print("ACCEPTANCE:" + json.dumps({{"visible": visible, "environment": dict(os.environ), "proc_leaks": proc_leaks,
    "shell": shell, "egress_socket": Path("/run/egress.sock").is_socket(),
    "predicate_rejected": rejected, "predicate_valid": evaluate("len(x) == 2", {{"x": [1, 2]}})}}))
''')
        assert not any(report["visible"].values()), report
        assert report["egress_socket"] is True, "only the restricted outbound broker socket should be visible"
        assert report["proc_leaks"] == [], report
        assert_no_values(report["environment"], [AMBIENT, BYOK[other]])
        assert all(report["environment"].get(key, "") == "" for key in
                   ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPERATOR_ENV_SENTINEL", "CHUDWARE_AGENT_AUTH_FILE")), report
        assert report["shell"]["ok"] is True and report["shell"]["exit_code"] == 0, report
        assert "actor-shell-ok" in report["shell"]["output"], report
        assert report["predicate_rejected"] == [True, True, True] and report["predicate_valid"] is True, report
        denial(ws.request("agent_spawn", {"command": "/usr/bin/id"}), "cell_refused")
        egress = probe(ws, '''
import json, urllib.request
try:
    urllib.request.urlopen("https://provider.example/acceptance", timeout=10)
    refused = False
except OSError as exc:
    refused = True
    error = str(exc)
print("ACCEPTANCE:" + json.dumps({"refused": refused, "error": error}))
''', "egress-probe")
        assert egress["refused"] is True, egress
        cell = h.running[h.principal(email).cell_id]
        assert cell.broker.wait_target() == "provider.example:443"
        before = len(cell.broker.targets)
        for method, params in (
            ("agent_resolve_provider", {"name": "openai"}),
            ("agent_resolve_provider", {"name": "anthropic"}),
            ("agent_answer_question", {"question": "Explain this box."}),
            ("agent_clarify", {"request": "Make a box."}),
            ("agent_pm_assist", {"params": {"type": "box", "length": 1, "width": 1, "height": 1}}),
        ):
            response = ws.request(method, params, timeout=30)
            assert_no_values(response, [AMBIENT, BYOK[A], BYOK[B]])
        assert len(cell.broker.targets) == before, cell.broker.targets[before:]
    print("SCENARIO sandbox: real model/shell/predicate/egress paths; no ambient/foreign env/files/proc; spawn refused")


def test_killing_a_cell_preserves_b_and_a_reopens_verified_warm(two_tenants):
    h = two_tenants
    a, b = h.socket(A), h.socket(B)
    aid, _ = box(a, "A-persisted")
    bid, _ = box(b, "B-live", length=20)
    saved = a.ok("doc_save", {"doc_id": aid, "path": "warm.step+"})
    assert saved["cache"]["written"] is True
    b.ok("doc_save", {"doc_id": bid, "path": "foreign.step+"})
    bpid = h.running[h.principal(B).cell_id].proc.pid
    h.kill(h.principal(A))
    b.ok("ping")
    assert h.running[h.principal(B).cell_id].proc.pid == bpid
    assert h.running[h.principal(B).cell_id].proc.poll() is None
    assert b.ok("doc_activate", {"doc_id": bid})["volume"] > 0
    resumed = h.socket(A)
    opened = resumed.ok("doc_open", {"path": "warm.step+"})
    assert opened["cache_hit"] is True and opened["warm"] is True, opened
    assert opened["cache_trust"] == "verified", opened
    assert opened["cache"]["features_executed"] == 0, opened
    activated = resumed.ok("doc_activate", {"doc_id": opened["doc_id"]})
    assert activated["rebuild"]["doc_status"]["features_executed"] == 0, activated
    # Copying an external file is a fixture import, not an alleged namespace escape.
    foreign_fixture = h.principal(A).root / "foreign-fixture.step+"
    foreign_fixture.write_bytes((h.principal(B).root / "foreign.step+").read_bytes())
    imported = resumed.ok("doc_open", {"path": "foreign-fixture.step+"})
    assert imported["cache_trust"] == "unverified", imported
    print("SCENARIO crash/persistence: B survives A SIGKILL; A restart warm cache_hit/0 features; foreign cache unverified")


def test_gateway_bridge_budget_is_per_actor_without_cross_tenant_charge(two_tenants, monkeypatch):
    h = two_tenants
    monkeypatch.setattr(h.gateway.settings, "max_engine_bridges_per_user", 1)
    personal = h.socket(A)
    personal.ok("ping")
    with pytest.raises(WebSocketDisconnect) as shared_refused:
        h.socket(A, h.team)
    assert shared_refused.value.code == 4429
    h.socket(B).ok("ping")
    with pytest.raises(WebSocketDisconnect) as refused:
        h.socket(A)
    assert refused.value.code == 4429
    print("SCENARIO quota/bridges: A personal/team actor cap 4429; B bridge budget unaffected")


def test_gateway_disk_quota_measures_real_cell_writes_without_cross_charge(two_tenants, monkeypatch):
    import gateway.plans as plans
    h = two_tenants
    studio = plans.PLANS["studio"]
    a, b, team_a, team_c = h.socket(A), h.socket(B), h.socket(A, h.team), h.socket(C, h.team)
    # This is logical owner accounting, not a claim about EDQUOT. Start the
    # actual state first, then leave 64 KiB for ordinary operation metadata.
    inventory = quota_plan(h)
    def owner_used(email):
        owner_key = "user:" + h.gateway.accounts.get_account(email).uid
        owner = next(row for row in inventory["owners"] if row["owner_key"] == owner_key)
        return sum(h.gateway.limits.dir_size_bytes(Path(record["path"]))
                   for group in owner["groups"] for record in group["roots"])
    cap = max(owner_used(email) for email in (A, B, C)) + 64 * 1024
    monkeypatch.setitem(plans.PLANS, "studio", replace(studio, limits=replace(studio.limits, storage_quota_bytes=cap)))
    personal_bytes = cap - owner_used(A) + 5000
    # Model files are real writes made by untrusted actor code, not upload route counters.
    written = a.request("run_model", {"name": "quota", "code": f'''
from pathlib import Path
Path("/work/over-quota").write_bytes(b"x" * {personal_bytes})
result = Box(1,1,1)
'''}, timeout=90)
    denial(written, "quota_exceeded")
    assert (h.principal(A).root / "over-quota").stat().st_size == personal_bytes
    response = a.request("doc_new", {"name": "over-quota"})
    assert_denied(response)
    assert "quota" in json.dumps(response).lower(), response
    b.ok("doc_new", {"name": "B-not-charged"})
    team_a.ok("doc_new", {"name": "A-team-not-charged"})
    root = h.principal(C, h.team).root
    assert root == h.principal(A, h.team).root
    shared_bytes = cap - owner_used(C) + 5000
    (root / "shared-over-quota").write_bytes(b"x" * shared_bytes)
    assert (root / "shared-over-quota").stat().st_size == shared_bytes
    for ws in (team_a, team_c):
        rejected = ws.request("doc_new", {"name": "shared-full"})
        assert_denied(rejected)
        assert "quota" in json.dumps(rejected).lower(), rejected
    assert h.gateway.accounts.get_account(A).credit_balance == 0
    assert h.gateway.accounts.get_account(B).credit_balance == 0
    print("SCENARIO quota/accounting: actual workspace/state/bucket bytes; personal/team separation; C combined owner charged; not hard-quota attestation")


def test_real_pinned_ai_credentials_control_and_egress(two_tenants):
    h = two_tenants
    if h.payload.chud is None:
        pytest.skip("AI/eval/active-agent control and BYOK rotation blocked: " + h.payload.chud_blocked)
    if h.payload.sdk_blocked:
        pytest.skip("AI/eval/active-agent control and BYOK rotation blocked: " + h.payload.sdk_blocked)
    a, b = h.socket(A), h.socket(B)
    handles = {A: a.ok("agent_handles_new"), B: b.ok("agent_handles_new")}
    for email, ws in ((A, a), (B, b)):
        ident, row = start_provider_turn(h, ws, handles[email], email)
        assert row.key == BYOK[email] and row.path == "/v1/responses"
        assert_no_upstream_keys(h)
        h.provider_wire.release(BYOK[email])
        response = ws.response(ident, timeout=45)
        assert "result" in response and response["result"].get("ok") is not False, response
        assert row.closed.wait(10) and row.completed
        assert_no_values(response, [AMBIENT, BYOK[A], BYOK[B]])
        assert h.gateway.accounts.get_account(email).credit_balance == 0
        ws.ok("agent_cancel", {"provider": "openai", "run_id": ""})
    assert_no_upstream_keys(h)
    print("SCENARIO AI: real pinned SDK and tenant cells stream official-wire SSE through real broker; only synthetic upstream fixture sees BYOK")


def test_active_real_agent_empty_cancel_and_team_manager_stop_only(two_tenants):
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("active agent control blocked: " + (h.payload.chud_blocked or h.payload.sdk_blocked))
    a, b = h.socket(A), h.socket(B)
    control_a, control_b = h.socket(A), h.socket(B)
    handles = {A: a.ok("agent_handles_new"), B: b.ok("agent_handles_new")}
    requests, rows = {}, {}
    for email, ws in ((A, a), (B, b)):
        requests[email], rows[email] = start_provider_turn(h, ws, handles[email], email)
    for method, field, extra in (
        ("agent_cancel", "run_id", {}), ("agent_steer", "run_id", {"text": "foreign steer"}),
        ("agent_answer", "run_id", {"batch_id": "foreign", "answers": []}),
        ("chudcode_close_session", "session_id", {}),
        ("chudcode_session_stats", "session_id", {}),
        ("session_handoff", "session_id", {"messages": []}),
    ):
        response = control_b.request(method, {field: handles[A][field], "provider": "openai", **extra})
        # IDs minted in A are unknown to B's hostd, not recorded foreign owners.
        # Serialized calls can hit B's own busy lane but never disclose A data.
        expected = "worker_lane_busy" if method in {
            "chudcode_close_session", "chudcode_session_stats", "session_handoff"} else "not_found"
        denial(response, expected)
        assert_no_values(response, [AMBIENT, BYOK[A], BYOK[B]])
    canceled = control_b.ok("agent_cancel", {"provider": "openai", "run_id": ""})
    assert_no_values(canceled, [handles[A]["run_id"], handles[A]["session_id"]])
    assert rows[B].closed.wait(10) and not rows[B].completed, "cancel did not abort upstream SSE"
    assert_stopped(b.response(requests[B], timeout=30))
    for method, extra in (("chudcode_close_session", {}), ("chudcode_session_stats", {}),
                          ("session_handoff", {"messages": []})):
        denial(control_b.request(method, {"session_id": handles[A]["session_id"],
                                         "provider": "openai", **extra}), "not_found")
    assert requests[A] not in a.replies, "B empty cancel stopped A's live provider-blocked run"
    own = control_a.ok("agent_cancel", {"provider": "openai", "run_id": handles[A]["run_id"]})
    assert own["cancelled"] is True and own["active"] is True, own
    assert rows[A].closed.wait(10) and not rows[A].completed
    assert_stopped(a.response(requests[A], timeout=30))
    editor, manager = h.socket(A, h.team), h.socket(C, h.team)
    team_handles = editor.ok("agent_handles_new")
    active, team_row = start_provider_turn(h, editor, team_handles, A)
    for method, field, extra in (
        ("agent_handles_new", "session_id", {}),
        ("agent_steer", "run_id", {"text": "manager cannot steer"}),
        ("agent_answer", "run_id", {"batch_id": "foreign", "answers": []}),
        ("chudcode_close_session", "session_id", {}),
        ("chudcode_session_stats", "session_id", {}),
        ("session_handoff", "session_id", {"messages": []}),
    ):
        denial(manager.request(method, {field: team_handles[field], "provider": "openai", **extra}), "not_found")
    stopped = manager.ok("agent_cancel", {"provider": "openai", "run_id": team_handles["run_id"]})
    assert stopped["cancelled"] is True and stopped["reason"] == "manager_stop", stopped
    assert stopped["control_delegate"]["actor_key"] == h.principal(C, h.team).actor_key, stopped
    assert team_row.closed.wait(10) and not team_row.completed
    terminal = assert_stopped(editor.response(active, timeout=30))
    assert terminal.get("turn", terminal)["stop_reason"] == "manager_stop", terminal
    assert_no_values(terminal, [AMBIENT, BYOK[C], BYOK[B]])
    assert_no_upstream_keys(h)
    print("SCENARIO active control: real A/B SSE runs; B empty cancel own-only; C stop-only with manager attribution; upstream disconnect observed")


def test_obscure_native_pump_fea_and_svg_path_sinks(two_tenants):
    """Exercise the non-generic native sinks, with successful own-root controls."""
    h = two_tenants
    a, b = h.socket(A), h.socket(B)
    a.ok("ping")
    b.ok("ping")
    pa, pb = h.principal(A), h.principal(B)
    curve = "flow,head\n0,50\n50,45\n100,30\n150,5\n"
    (pa.root / "pump.csv").write_text(curve)
    (pb.root / "pump.csv").write_text(curve)
    system = {"static_head_m": 20.0, "k": 0.002}
    own = a.ok("piping_operating_point", {"pump_curve": "pump.csv", "system": system})
    assert own["ok"] is True and own["duty_point"]["flow_m3h"] > 0, own
    (pa.root / "foreign-pump").symlink_to(pb.root / "pump.csv")
    for path in (str(pb.root / "pump.csv"), "../" + pb.root.name + "/pump.csv", "foreign-pump"):
        assert_denied(a.request("piping_operating_point", {"pump_curve": path, "system": system}))

    study = {
        "points": [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                   0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1],
        "tets": [0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
                 0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6],
        "material": {"E_mpa": 200000.0, "nu": 0.29, "yield_mpa": 370.0},
        "fixtures": [{"points": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], "tol_mm": 0.01}],
        "loads": [{"type": "force", "vector": [0, 0, 50000],
                   "faces": {"points": [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], "tol_mm": 0.01}}],
    }
    a.ok("sim_run_static", {"study": {**study, "export_system_path": "own-system.npz"}})
    assert (pa.root / "own-system.npz").is_file()
    marker = pb.root / "system.npz"
    marker.write_bytes(b"B-FEA-marker")
    (pa.root / "foreign-system").symlink_to(marker)
    for path in (str(marker), "../" + pb.root.name + "/system.npz", "foreign-system"):
        assert_denied(a.request("sim_run_static", {"study": {**study, "export_system_path": path}}))
    assert marker.read_bytes() == b"B-FEA-marker"

    output = pa.root / "svg"
    output.mkdir()
    doc = {"schema": "aicad.sch/1", "name": "Schematic"}
    base = {"doc": doc, "kind": "svg", "path": "svg"}
    own_svg = a.ok("ecad_export", {**base, "opts": {"sheet": "sh1"}})
    own_leaf = own_svg["paths"][0].split("/")[-1]
    assert "<svg" in (output / own_leaf).read_text()
    foreign_svg = pb.root / "proof.svg"
    foreign_svg.write_bytes(b"B-SVG-marker")
    for sheet in (str(foreign_svg), "x/../../" + pb.root.name + "/proof"):
        assert_denied(a.request("ecad_export", {**base, "opts": {"sheet": sheet}}))
    (output / own_leaf).unlink()
    (output / own_leaf).symlink_to(foreign_svg)
    assert_denied(a.request("ecad_export", base))
    assert foreign_svg.read_bytes() == b"B-SVG-marker"
    b.ok("ping")
    print("SCENARIO native sinks: own pump CSV/FEA NPZ/SVG succeed; foreign/traversal/symlink sinks refused atomically")


def test_same_part_actor_writers_preserve_history_order_and_attribution(two_tenants):
    """A and C write the shared part through distinct real actor sandboxes."""
    h = two_tenants
    editor, viewer, manager = h.socket(A, h.team), h.socket(B, h.team), h.socket(C, h.team)
    ident, initial = box(editor, "shared-human-editable-history")
    viewer.ok("attach_doc", {"doc_id": ident, "mode": "ro"})
    viewer.ok("subscribe", {"doc_ids": [ident]})
    store = {"version": 2, "active": "private", "sessions": [{"id": "private", "marker": "A-private-chat-marker"}]}
    saved = h.client.put("/workspace/chat", headers={"x-dev-user": A}, json=store)
    assert saved.status_code == 200, saved.text
    private = probe(editor, f'''
import json
from pathlib import Path
from services.agent.request_context import actor_hex
Path("/actor/private-credential.json").write_text({BYOK[A]!r})
print("ACCEPTANCE:" + json.dumps({{"hex": actor_hex()}}))
''', "private-credential")

    def write(ws, index):
        return probe(ws, f'''
import json, time
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from services.mcp.fs_tools import register_shell_tools
from services.mcp.daemon_bridge import DaemonClient
from services.agent.request_context import current_context
client = DaemonClient()
try:
    client.connect(spawn=False)
    attached = client.call("attach_doc", {{"doc_id": {ident!r}, "mode": "rw"}})
    assert not attached.get("_is_error"), attached
    Path("/work/writer-{index}.ready").touch()
    deadline = time.monotonic() + 15
    while not Path("/work/writer-{3 - index}.ready").exists():
        assert time.monotonic() < deadline, "other real actor did not arrive"
        time.sleep(0.02)
    reply = client.call("doc_apply_ops", {{"doc_id": {ident!r}, "base_revision": -1,
        "ops": [{{"op": "add_feature", "feature": {{"type": "box", "name": "agent-history-{index}",
            "params": {{"length": {13 if index == 1 else 10}, "width": {14 if index == 2 else 10}, "height": 10}}}}}}]}})
finally:
    client.close()
mcp = FastMCP("acceptance-writer-shell")
register_shell_tools(mcp)
shell = mcp._tool_manager.get_tool("run_shell").fn(
    command="test ! -e /state/actors/{private['hex']}/private-credential.json" + (
        " && test ! -e /actor/private-credential.json" if {index} == 2 else ""),
    cwd="/work", timeout_s=10)
print("ACCEPTANCE:" + json.dumps({{"reply": reply, "actor": current_context()["actor_key"], "shell": shell}}))
''', f"same-part-writer-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, ws, index) for index, ws in ((1, editor), (2, manager))]
        reports = [future.result(timeout=120) for future in futures]
    assert {report["actor"] for report in reports} == {
        h.principal(A, h.team).actor_key, h.principal(C, h.team).actor_key}
    for report in reports:
        assert not report["reply"].get("_is_error"), json.dumps(report)
        assert all(feature["ok"] for feature in report["reply"]["rebuild"]["feature_status"]), json.dumps(report)
        assert report["shell"]["ok"] is True and report["shell"]["exit_code"] == 0, report
        assert_no_values(report, [BYOK[A], BYOK[C]])
    revisions = sorted(report["reply"]["revision"] for report in reports)
    assert revisions == [initial["revision"] + 1, initial["revision"] + 2], json.dumps(reports)
    features = editor.ok("doc_tree", {"doc_id": ident})["tree"]["features"]
    arrival = sorted(reports, key=lambda report: report["reply"]["revision"])
    assert [feature["name"] for feature in features[-2:]] == [
        report["reply"]["tree"]["features"][-1]["name"] for report in arrival]
    assert {feature["name"] for feature in features[-2:]} == {"agent-history-1", "agent-history-2"}
    for report in reports:
        revision = report["reply"]["revision"]
        event = viewer.wait_event(lambda event: event.get("event") == "doc.changed" and
            event.get("data", {}).get("doc_id") == ident and event["data"].get("revision") == revision)
        assert event["data"]["user"]["id"] == report["actor"], event
        assert event["data"]["actor_key"] == report["actor"], event
    loaded = h.client.get("/workspace/chat", headers={"x-dev-user": C})
    assert loaded.status_code == 200 and "A-private-chat-marker" not in loaded.text, loaded.text
    print("SCENARIO same part: A/C actor batches retain both features in arrival order and attribution; private chat/credentials hidden")


def test_gateway_oversized_frame_is_actor_local(two_tenants, monkeypatch):
    h = two_tenants
    monkeypatch.setattr(h.gateway.settings, "ws_max_message_bytes", 8192)
    a, b = h.socket(A), h.socket(B)
    a.ok("ping")
    b.ok("ping")
    a.ws.send_text(json.dumps({"id": 999, "method": "ping", "params": {"oversized": "x" * 9000}}))
    a.wait_closed(1009)
    b.ok("ping")
    h.socket(A).ok("ping")
    print("SCENARIO quota/frames: A oversized frame closes only A bridge 1009; B and a new A bridge remain usable")


def test_real_pinned_chud_bash_has_only_actor_namespace(two_tenants):
    """Local RPC bash is executable without an LLM or a successful provider call."""
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("real chud bash blocked: " + (h.payload.chud_blocked or h.payload.sdk_blocked))
    for email, other in ((A, B), (B, A)):
        ws = h.socket(email)
        cell = h.running[h.principal(email).cell_id]
        before = len(cell.broker.targets)
        command = "/opt/chudware/venv/bin/python -I -c " + shlex.quote(f'''
import json, os
from pathlib import Path
paths = {["/state", "/run/gateway.sock", str(h.ambient_file), str(h.principal(other).root)]!r}
leaks = []
for entry in Path("/proc").iterdir():
    if entry.name.isdigit():
        try:
            raw = (entry / "environ").read_bytes()
            if {AMBIENT!r}.encode() in raw or {BYOK[other]!r}.encode() in raw:
                leaks.append(entry.name)
        except OSError:
            pass
Path("/work/chud-bash-report.json").write_text(json.dumps({{"env": dict(os.environ),
    "visible": {{path: Path(path).exists() for path in paths}}, "proc_leaks": leaks}}))
print("bash-report-ready")
''')
        report = probe(ws, f'''
import json
from dataclasses import asdict
from chudcode_rpc import RpcClient
client = RpcClient(executable="/opt/chudware/bin/chud", provider="openai", model="gpt-4o-mini",
    cwd="/work", no_session=True, no_skills=True, no_rules=True, no_ui=True,
    tools=["bash"], extra_args=("--no-lsp", "--no-mcp"), startup_timeout=30, request_timeout=30)
try:
    client.start()
    answer = asdict(client.bash({command!r}))
finally:
    client.stop()
print("ACCEPTANCE:" + json.dumps({{"answer": answer,
    "evidence": json.loads(__import__("pathlib").Path("/work/chud-bash-report.json").read_text())}}))
''', "real-chud-bash")
        assert report["answer"]["exit_code"] == 0 and report["answer"]["cancelled"] is False, report
        visible = report["evidence"]
        assert not any(visible["visible"].values()) and visible["proc_leaks"] == [], visible
        assert_no_values(visible["env"], [AMBIENT, BYOK[other]])
        assert all(not visible["env"].get(key) for key in
                   ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPERATOR_ENV_SENTINEL")), visible
        assert "api.openai.com:443" not in cell.broker.targets[before:], cell.broker.targets
    print("SCENARIO real chud bash: pinned RPC binary executes in each real actor; no ambient/foreign env/files/proc or provider call")


def test_transient_cell_cgroup_pids_memory_oom_does_not_kill_sibling(two_tenants, monkeypatch):
    """One auto-collected scratch scope; no persistent or existing unit changes."""
    h = two_tenants
    if not shutil.which("systemd-run") or not Path(f"/run/user/{os.getuid()}/bus").is_socket():
        pytest.skip("runtime cgroup/OOM leg needs an accessible user manager for a transient scratch scope")
    # The suite's private runtime dir is not the actual user-manager transport.
    runtime = f"/run/user/{os.getuid()}"
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    b = h.socket(B)
    bid, _ = box(b, "B-survives-A-resource-ceilings")
    principal = h.principal(A)
    scope = "tc-acceptance-" + uuid.uuid4().hex + ".scope"
    h.scopes[principal.cell_id] = scope
    a = h.socket(A)
    cell = h.running[principal.cell_id]
    shown = subprocess.run(["systemctl", "--user", "show", scope, "--property=ControlGroup", "--value"],
                           capture_output=True, text=True, timeout=10)
    assert shown.returncode == 0 and scope in shown.stdout, (shown.stdout, shown.stderr)
    cgroup = Path("/sys/fs/cgroup") / shown.stdout.strip().lstrip("/")
    assert (cgroup / "memory.max").read_text().strip() == str(1536 * 1024 * 1024)
    assert (cgroup / "memory.swap.max").read_text().strip() == "0"
    assert (cgroup / "pids.max").read_text().strip() == "128"
    quota, period = map(int, (cgroup / "cpu.max").read_text().split())
    assert quota == period
    before_pids = dict(line.split() for line in (cgroup / "pids.events").read_text().splitlines())
    exhausted = probe(a, '''
import errno, json, subprocess
children = []
failure = None
try:
    for _ in range(200):
        try:
            children.append(subprocess.Popen(["/usr/bin/sleep", "60"]))
        except OSError as exc:
            failure = exc.errno
            break
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait(timeout=10)
print("ACCEPTANCE:" + json.dumps({"children": len(children), "errno": failure}))
''', "pids-ceiling")
    assert exhausted["errno"] == 11 and 0 < exhausted["children"] < 128, exhausted
    after_pids = dict(line.split() for line in (cgroup / "pids.events").read_text().splitlines())
    assert int(after_pids["max"]) > int(before_pids["max"])
    a.ok("ping")
    b.ok("ping")
    assert b.ok("doc_activate", {"doc_id": bid})["volume"] > 0
    with (cgroup / "memory.events").open() as events:
        before_memory = dict(line.split() for line in events.read().splitlines())
        stopped = a.request("run_model", {"name": "oom-ceiling", "code": '''
from pathlib import Path
Path("/work/oom-started").touch()
allocations = []
while True:
    allocations.append(bytearray(32 * 1024 * 1024))
'''}, timeout=60)
        assert_denied(stopped)
        events.seek(0)
        after_memory = dict(line.split() for line in events.read().splitlines())
    assert (principal.root / "oom-started").exists(), a.diagnostics()
    assert int(after_memory["oom_kill"]) > int(before_memory["oom_kill"]), after_memory
    assert_no_values(stopped, [AMBIENT, BYOK[B]])
    b.ok("ping")
    assert b.ok("doc_activate", {"doc_id": bid})["volume"] > 0
    assert h.running[h.principal(B).cell_id].proc.poll() is None
    print("SCENARIO runtime quota: isolated transient CPU/memory/swap/pids ceilings; pids EAGAIN and A execution OOM leave B/harness alive")


def test_real_pinned_sdk_catalog_is_broker_scoped_filtered_and_revocable(two_tenants):
    """Native metadata clients, not gateway presets or an echoed model fixture."""
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("real cell catalog needs pinned native SDK: " +
                    (h.payload.chud_blocked or h.payload.sdk_blocked))
    a, b = h.socket(A), h.socket(B)
    grants = {
        email: {provider: h.gateway.provider_broker.mint_grant(email, h.principal(email), provider)
                for provider in ("openai", "anthropic")}
        for email in (A, B)
    }
    forbidden = [AMBIENT, *BYOK.values(),
                 *(grant for actor_grants in grants.values() for grant in actor_grants.values())]

    def availability(email):
        response = h.client.get("/api/providers", headers={"x-dev-user": email})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["shared_host_accounts"] is False, payload
        assert_no_values(payload, forbidden)
        return {row["id"] for row in payload["providers"]}

    def saved_sessions():
        return {str(path) for cell in h.running.values()
                for root in (cell.directory / "state", cell.principal.root)
                for path in root.rglob("*.jsonl") if "Sessions" in path.parts}

    sessions_before = saved_sessions()
    wire_offset = len(h.provider_wire.records)
    assert availability(A) == availability(B) == {"openai", "anthropic"}

    # Both a direct foreign capability and a nested reserved map must fail at
    # gateway JSON admission, before a native catalog helper can consume them.
    for params in ({"_provider_catalog": grants[B]},
                   {"options": [{"_provider_catalog": grants[B]}]}):
        a.send("chudcode_models_list", params)
        refusal = a.response(None)
        denial(refusal, -32700)
        assert_no_values(refusal, forbidden)

    expected = {("openai", "gpt-4o-mini"), ("anthropic", "claude-sonnet-4-5")}
    fields = {"id", "name", "provider", "contextWindow", "maxTokens", "reasoning",
              "reasoningEfforts", "vision", "input", "cost"}

    def catalog(ws, provider=None):
        result = ws.ok("chudcode_models_list",
                       {"providerId": provider} if provider else {}, timeout=90)
        assert result["ok"] is True and result["refreshing"] is False, result
        assert isinstance(result["models"], list), result
        assert_no_values(result, forbidden)
        assert "cpg1." not in json.dumps(result) and "127.0.0.1:18081" not in json.dumps(result)
        for row in result["models"]:
            assert set(row) == fields, row
            assert row["provider"] in {"openai", "anthropic"}, row
            assert isinstance(row["vision"], bool) and isinstance(row["reasoningEfforts"], list), row
            if (row["provider"], row["id"]) in expected:
                assert row["contextWindow"] > 0 and row["maxTokens"] > 0, row
        return result["models"]

    initial_a, initial_b = catalog(a), catalog(b)
    assert expected <= {(row["provider"], row["id"]) for row in initial_a}
    assert expected <= {(row["provider"], row["id"]) for row in initial_b}
    for provider in ("openai", "anthropic"):
        filtered = catalog(a, provider)
        assert filtered == [row for row in initial_a if row["provider"] == provider]
        assert filtered, f"native {provider} catalog unexpectedly empty"
    assert catalog(a, "xai") == []
    assert_no_upstream_keys(h, *(grant for values in grants.values() for grant in values.values()))

    # Gateway availability and a new native catalog call both use current key
    # authority, even after a successful catalog populated native metadata.
    for provider, remaining in (("openai", {"anthropic"}), ("anthropic", set())):
        removed = h.client.delete(f"/api/providers/keys/{provider}", headers={"x-dev-user": A})
        assert removed.status_code == 200 and removed.json() == {"ok": True}, removed.text
        assert availability(A) == remaining
        assert availability(B) == {"openai", "anthropic"}
        assert catalog(a, provider) == []
        after = catalog(a)
        assert {row["provider"] for row in after} == remaining
        assert catalog(b) == initial_b, "A key deletion changed B's genuine native catalog"

    assert saved_sessions() == sessions_before, "metadata discovery persisted a conversation"
    assert len(h.provider_wire.records) == wire_offset, "catalog discovery started a provider turn"
    assert_no_values(a.events + b.events, forbidden)
    # Replies arrive only after short-lived metadata helpers stop and remove
    # their credential scope; neither keys nor retired grants may remain there.
    assert_no_upstream_keys(h, *(grant for values in grants.values() for grant in values.values()))
    print("SCENARIO catalog: actual pinned SDK OpenAI/Anthropic metadata, provider filters, gateway availability and actor-local key revocation; forged maps rejected before runtime")


def test_byok_rotation_preserves_owned_cancel_and_conversation_handle(two_tenants):
    """Rotation must not orphan the actor's already-running session."""
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("BYOK rotation blocked: " + (h.payload.chud_blocked or h.payload.sdk_blocked))
    a, control, b = h.socket(A), h.socket(A), h.socket(B)
    handles = a.ok("agent_handles_new")
    principal = h.principal(A)
    grant = h.gateway.provider_broker.mint_grant(A, principal, "openai")
    active, original = start_provider_turn(h, a, handles, A)
    session = a.wait_event(lambda event: event.get("event") == "agent.event" and
                           event.get("data", {}).get("kind") == "agent.session")["data"]["chudcode_session_id"]
    assert session == handles["session_id"] and session != handles["run_id"]
    rotated = "sk-fake-acceptance-rotated-A-never-a-provider-key"
    h.provider_wire.allow_key(rotated)
    h.gateway.provider_keys._persist_validated_key(A, "openai", rotated)
    assert h.gateway.provider_broker.mint_grant(A, h.principal(A), "openai") == grant
    canceled = control.ok("agent_cancel", {"run_id": handles["run_id"], "provider": "openai"})
    assert canceled["cancelled"] is True and canceled["active"] is True, canceled
    assert original.closed.wait(10) and not original.completed
    assert_stopped(a.response(active, timeout=30))
    native_session = h.native_session_identity(principal.cell_id)
    b.ok("ping")
    continued = a.ok("agent_handles_new", {"session_id": handles["session_id"]})
    assert continued["session_id"] == handles["session_id"] and continued["run_id"] != handles["run_id"]
    offset = len(a.events)
    resumed, latest = start_provider_turn(h, a, continued, A, key=rotated,
                                          request="Continue the existing conversation.")
    a.wait_event(lambda event: event.get("event") == "agent.event" and
                 event.get("data", {}).get("kind") == "agent.session" and
                 event["data"].get("run_id") == continued["run_id"])
    bound = [event["data"]["chudcode_session_id"] for event in a.events[offset:]
             if event.get("event") == "agent.event" and event.get("data", {}).get("kind") == "agent.session"]
    assert bound and all(value == session for value in bound), "key rotation or cancel/resume changed the conversation handle"
    assert h.native_session_identity(principal.cell_id) == native_session
    assert latest.key == rotated
    assert_no_upstream_keys(h, rotated)
    # Deleting BYOK must never prevent a Stop on the still-active owned run.
    assert h.gateway.provider_keys.delete_key(A, "openai")
    stopped = control.ok("agent_cancel", {"run_id": continued["run_id"], "provider": "openai"})
    assert stopped["cancelled"] is True, stopped
    assert latest.closed.wait(10) and not latest.completed
    assert_stopped(a.response(resumed, timeout=30))
    assert_no_values(a.events, [AMBIENT, *BYOK.values(), rotated])
    assert_no_upstream_keys(h, rotated)
    print("SCENARIO rotation/cancel: same grant/native session, latest key only at fixture, Stop remains authorized after key deletion")


def test_real_signed_thinking_cache_prefix_and_native_session_replay(two_tenants):
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("real signed-thinking replay needs pinned native SDK: " +
                    (h.payload.chud_blocked or h.payload.sdk_blocked))
    a = h.socket(A)
    handles = a.ok("agent_handles_new")
    first, initial = start_provider_turn(h, a, handles, A, provider="anthropic",
                                         request="First local replay turn; no tools.")
    bound = a.wait_event(lambda event: event.get("event") == "agent.event" and
                        event.get("data", {}).get("kind") == "agent.session")["data"]
    session_handle = bound["chudcode_session_id"]
    assert session_handle == handles["session_id"] and session_handle != handles["run_id"]
    h.provider_wire.release(BYOK[A])
    response = a.response(first, timeout=45)
    assert response.get("result", {}).get("ok") is True, response
    assert initial.completed and initial.closed.wait(10)
    native_session = h.native_session_identity(h.principal(A).cell_id)
    continued = a.ok("agent_handles_new", {"session_id": handles["session_id"]})
    next_turn, replay = start_provider_turn(h, a, continued, A, provider="anthropic",
                                          request="Second local replay turn; preserve context; no tools.")
    second_bound = a.wait_event(lambda event: event.get("event") == "agent.event" and
                               event.get("data", {}).get("kind") == "agent.session" and
                               event["data"].get("run_id") == continued["run_id"])["data"]
    assert second_bound["chudcode_session_id"] == session_handle
    assert h.native_session_identity(h.principal(A).cell_id) == native_session
    assert replay.body["system"] == initial.body["system"], "stable system/cache prefix changed"
    assert replay.body.get("tools") == initial.body.get("tools"), "stable tools/cache prefix changed"
    assert '"cache_control"' in json.dumps(initial.body["system"]) + json.dumps(initial.body.get("tools")), (
        "native SDK did not mark a provider-cache prefix")
    assistant = [message for message in replay.body["messages"] if message["role"] == "assistant"]
    blocks = [block for message in assistant for block in message["content"] if isinstance(block, dict)]
    assert any(block.get("type") == "thinking" and block.get("thinking") == THINKING
               and block.get("signature") == SIGNATURE for block in blocks)
    assert any(block.get("type") == "text" and block.get("text") == TEXT for block in blocks)
    assert_no_upstream_keys(h)
    h.provider_wire.release(BYOK[A])
    terminal = a.response(next_turn, timeout=45)
    assert terminal.get("result", {}).get("ok") is True, terminal
    assert replay.completed and replay.closed.wait(10)
    assert_no_values(a.events, [AMBIENT, *BYOK.values()])
    print("SCENARIO replay: actual pinned SDK preserves same native session, exact fixture thinking/signature/text and stable marked cache prefix; not actual cache-hit/signature authority")


def test_real_provider_revocation_closes_sse_and_stale_crosssocket_grants_refuse(two_tenants):
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("real provider revocation needs pinned native SDK: " +
                    (h.payload.chud_blocked or h.payload.sdk_blocked))
    a, b = h.socket(A), h.socket(B)
    # hostd clientLoop dispatches synchronously; a held turn owns its connection.
    # Independently admitted same-actor sockets exercise real concurrent refusal.
    probe_a, probe_b = h.socket(A), h.socket(B)
    handles_a, handles_b = a.ok("agent_handles_new"), b.ok("agent_handles_new")
    active_a, row_a = start_provider_turn(h, a, handles_a, A)
    active_b, row_b = start_provider_turn(h, b, handles_b, B)
    old_principal = h.principal(A)
    grant_a = h.gateway.provider_broker.mint_grant(A, old_principal, "openai")
    # A genuine actor sandbox on B cannot spend A's still-current grant over B's socket.
    offset = len(h.provider_wire.records)
    def refused(ws, grant, path="/openai/v1/responses"):
        report = probe(ws, f'''
import json, urllib.request, urllib.error
request = urllib.request.Request("http://127.0.0.1:18081{path}",
    data=b'{{"model":"gpt-4o-mini","input":"must-not-reach-upstream","stream":true}}',
    headers={{"Authorization":"Bearer " + {grant!r}, "Content-Type":"application/json"}})
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as exc:
    print("ACCEPTANCE:" + json.dumps({{"status":exc.code}}))
else:
    raise AssertionError("foreign/stale provider grant accepted")
''', "provider-grant-refusal")
        assert report["status"] in (401, 403, 404)
    def assert_original_streams_held():
        for ws, active, row in ((a, active_a, row_a), (b, active_b, row_b)):
            assert active not in ws.replies and not row.hold.is_set()
            assert not row.completed and not row.closed.is_set(), "grant probe interrupted an original stream"

    assert_original_streams_held()
    refused(probe_b, grant_a)
    assert_original_streams_held()
    refused(probe_a, grant_a, path="/http://169.254.169.254/")
    assert_original_streams_held()
    refused(probe_a, grant_a, path="/anthropic/v1/messages")
    assert_original_streams_held()
    assert len(h.provider_wire.records) == offset
    assert h.gateway.provider_keys.delete_key(A, "openai")
    assert row_a.closed.wait(10) and not row_a.completed, "key revocation left upstream stream alive"
    result = a.response(active_a, timeout=45)
    assert_denied(result)
    assert active_b not in b.replies and not row_b.closed.is_set(), "A revocation interrupted B"
    refused(probe_a, grant_a)
    assert active_b not in b.replies and not row_b.closed.is_set() and not row_b.completed
    # Rotate/recreate the key but advance principal authority: the old grant stays stale.
    h.gateway.provider_keys._persist_validated_key(A, "openai", BYOK[A])
    h.gateway.cells.revoke(email=A)
    a.wait_closed()
    probe_a.wait_closed()
    fresh_a = h.socket(A)
    assert h.principal(A).epoch != old_principal.epoch
    refused(fresh_a, grant_a)
    current_principal = h.principal(A)
    # A delayed duplicate update cannot retire the newly admitted generation.
    asyncio.run(h.gateway.engine_bridge._principal_control(
        current_principal, current_principal.epoch, current_principal))
    refused(fresh_a, grant_a)
    # Nor can an older control handshake restore its stale authority.
    asyncio.run(h.gateway.engine_bridge._principal_control(
        old_principal, old_principal.epoch, old_principal))
    refused(fresh_a, grant_a)
    assert active_b not in b.replies and not row_b.closed.is_set() and not row_b.completed
    assert len(h.provider_wire.records) == offset
    h.provider_wire.release(BYOK[B])
    terminal_b = b.response(active_b, timeout=45)
    assert terminal_b.get("result", {}).get("ok") is True, terminal_b
    assert row_b.completed
    assert_no_upstream_keys(h)
    print("SCENARIO revoke: real broker closes A SSE on key deletion, B continues; current crosssocket and old-epoch grants never reach upstream")


@pytest.mark.parametrize("workspace", ["personal", "shared-team"])
def test_legacy_provider_auth_aliases_cannot_change_another_actors_credentials(two_tenants, workspace):
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("actual SDK aliases require the pinned acceptance runtime")
    team = h.team if workspace == "shared-team" else ""
    peer = C if team else B
    actor, other = h.socket(A, team), h.socket(peer, team)
    handles = other.ok("agent_handles_new")
    active, row = start_provider_turn(h, other, handles, peer)
    try:
        for method, params in (
            ("chudcode_set_key", {"providerId": "anthropic",
                                  "apiKey": "sk-fake-alias-replacement-never-a-provider-key"}),
            ("chudcode_remove_key", {"providerId": "anthropic"}),
            ("chudcode_logout", {"providerId": "anthropic"}),
        ):
            denial(actor.request(method, params), "credential_isolation_unsupported")
            assert active not in other.replies and not row.closed.is_set() and not row.completed
    finally:
        h.provider_wire.release(BYOK[peer])
    completed = other.response(active, timeout=45)
    assert "result" in completed and completed["result"].get("ok") is not False, completed
    assert row.closed.wait(10) and row.completed and row.key == BYOK[peer]
    resumed = other.ok("agent_handles_new", {"session_id": handles["session_id"]})
    second, replay = start_provider_turn(h, other, resumed, peer)
    h.provider_wire.release(BYOK[peer])
    terminal = other.response(second, timeout=45)
    assert "result" in terminal and terminal["result"].get("ok") is not False, terminal
    assert replay.closed.wait(10) and replay.completed and replay.key == BYOK[peer]
    stats = other.ok("chudcode_session_stats", {"session_id": handles["session_id"]})
    assert stats["userMessages"] >= 2 and stats["assistantMessages"] >= 2, stats
