"""Regression coverage for shared CAD result, recovery and job plumbing."""
import json
import threading

from services.mcp import cad_common as common
from services.mcp import daemon_bridge


def test_error_with_foreign_edit_is_valid_json(monkeypatch):
    edits = [{"doc_id": "doc-a", "source_kind": "gui", "revision": 5,
              "method": "doc_apply_ops"}]
    monkeypatch.setattr(common.cb, "consume_foreign_edits", lambda: edits)
    result = json.loads(common._err(ValueError("failed")))
    assert result["ok"] is False
    assert result["foreign_edits"][0]["doc_id"] == "doc-a"


def test_job_records_baseexception_and_serializes_document(monkeypatch):
    monkeypatch.setattr(common, "_capture_turn_lease", lambda: None)
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    def first():
        entered.set()
        assert release.wait(5)
        raise SystemExit("failed")
    one = common._start_job("first", first, doc_id="doc-sequential")
    try:
        assert entered.wait(5)
        two = common._start_job("second", lambda: second.set() or {"ok": True},
                                doc_id="doc-sequential")
        assert not second.wait(0.05)
        release.set()
        assert second.wait(5)
        with common._JOB_LOCK:
            assert common._JOBS[one]["status"] == "error"
            assert "SystemExit" in common._JOBS[one]["error"]
    finally:
        release.set()


def test_stainless_family_default_and_registry_values(monkeypatch):
    monkeypatch.setattr(common, "_material_registry_record", lambda name: None)
    props, assumptions = common.material_thermal_props("stainless steel")
    assert props == {"conductivity_w_mk": 16.0, "cp_j_kgk": 500.0}
    assert all(a["source"] == "family default" for a in assumptions)
    monkeypatch.setattr(common, "_material_registry_record", lambda name: {
        "property_basis": {"thermal_conductivity_w_m_k": 24.7,
                           "specific_heat_j_kg_k": 460}})
    props, assumptions = common.material_thermal_props("rotor stainless")
    assert props == {"conductivity_w_mk": 24.7, "cp_j_kgk": 460.0}
    assert assumptions == []


def test_recovery_remap_never_selects_arbitrary_document(monkeypatch):
    class Client:
        server_info = {"id_remap": {"old-a": "new-a", "old-b": "new-b"}}
        def call(self, method, *args):
            return {"recovery": "done"}
        def refresh_server_info(self):
            pass
    monkeypatch.setattr(daemon_bridge, "client", lambda: Client())
    monkeypatch.setattr(common.cb, "call", lambda method, params: {"docs": []})
    monkeypatch.setattr(common, "_DOC_ALIASES", {})
    monkeypatch.setitem(common._CUR, "doc_id", None)
    recovered = common._recover_via_daemon()
    assert len(recovered) == 2
    assert common._CUR["doc_id"] is None
    assert common._DOC_ALIASES == {"old-a": "new-a", "old-b": "new-b"}


def test_recovery_selects_newest_capture_not_last_opened(monkeypatch):
    class Client:
        def call(self, method, params=None):
            if method == "recovery_list":
                return {"entries": [{"id": "older", "captured_at": 10},
                                    {"id": "newer", "captured_at": 20}]}
            return {"opened": [{"doc_id": params["id"], "original_doc_id": ""}]}
    monkeypatch.setattr(daemon_bridge, "client", lambda: Client())
    monkeypatch.setitem(common._CUR, "doc_id", None)
    common._recover_orphans_via_daemon()
    assert common._CUR["doc_id"] == "newer"


def test_resolution_row_is_used_once(monkeypatch):
    bridge = common.cb
    bridge.remember_resolution_rows([{"doc_id": "d", "revision": 4}])
    assert bridge._take_resolution_row("d")["revision"] == 4
    assert bridge._take_resolution_row("d") is None
    bridge.remember_resolution_rows([{"doc_id": "d", "busy": True, "revision": 4}])
    assert bridge._take_resolution_row("d") is None
