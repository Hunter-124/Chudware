"""The MCP reply budget: tool_reply keeps every JSON reply readable by the client."""
from __future__ import annotations

import json

from services.mcp import _envelopes
from services.mcp.cad_eval_tools import (_compact_mass_bodies, _mass_warnings,
                                         compact_interference, promote_touching, touching_note)


def _big(n: int = 3000) -> dict:
    return {"ok": True, "doc_id": "d1", "revision": 7, "body_count": n,
            "summary": "built", "rows": [{"id": i, "pad": "x" * 40} for i in range(n)],
            "blob": "y" * 200_000}


def test_small_reply_is_untouched(monkeypatch):
    monkeypatch.delenv(_envelopes.REPLY_BUDGET_ENV, raising=False)
    text = json.dumps({"ok": True, "rows": [1, 2, 3]}, indent=2)
    assert _envelopes.tool_reply(text, "measure_part") == text
    small = {"ok": True, "rows": [1, 2, 3]}
    assert _envelopes.tool_reply(small) == small


def test_oversized_json_string_is_shrunk_keeping_verdict(monkeypatch):
    monkeypatch.delenv(_envelopes.REPLY_BUDGET_ENV, raising=False)
    reply = _envelopes.tool_reply(json.dumps(_big(), indent=2), "add_features")
    assert isinstance(reply, str) and len(reply) <= _envelopes.DEFAULT_REPLY_BUDGET
    out = json.loads(reply)
    assert (out["ok"], out["doc_id"], out["revision"], out["body_count"]) == (True, "d1", 7, 3000)
    assert out["_truncated"]["rows"] == 3000
    assert out["_truncated"]["blob(chars)"] == 200_000
    assert "get_part(summary=True)" in out["truncated"]  # the tool's own narrowing hint
    assert len(out["rows"]) < 3000


def test_oversized_dict_reply_is_shrunk(monkeypatch):
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "20000")
    out = _envelopes.tool_reply(_big(), "check_interference")
    assert isinstance(out, dict)
    assert len(json.dumps(out, separators=(",", ":"))) <= 20000
    assert out["ok"] is True and out["revision"] == 7
    assert "clearance_offset" in out["truncated"]


def test_whitespace_only_overflow_is_compacted_without_a_note(monkeypatch):
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "5000")
    payload = {"ok": True, "rows": [[i, i + 1, i + 2] for i in range(300)]}
    indented = json.dumps(payload, indent=2)
    assert len(indented) > 5000
    out = json.loads(_envelopes.tool_reply(indented))
    assert out == payload


def test_error_reply_keeps_error_and_code(monkeypatch):
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "3000")
    out = json.loads(_envelopes.tool_reply(json.dumps(
        {"ok": False, "error": "boom", "code": "bad_thing", "rows": list(range(5000))})))
    assert out["ok"] is False and out["error"] == "boom" and out["code"] == "bad_thing"
    assert out["_truncated"]["rows"] == 5000


def test_last_resort_keeps_only_protected_fields(monkeypatch):
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "2000")
    payload = {"ok": True, "revision": 3, **{f"k{i}": "z" * 70 for i in range(200)}}
    out = json.loads(_envelopes.tool_reply(json.dumps(payload)))
    assert out["ok"] is True and out["revision"] == 3
    assert "Every other field was dropped" in out["truncated"]


def test_non_json_and_disabled_budget_pass_through(monkeypatch):
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "100")
    text = "plain " * 1000
    assert _envelopes.tool_reply(text) == text
    caption = ["<image>", "x" * 1000]
    assert _envelopes.tool_reply(caption) is caption
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "0")
    big = json.dumps(_big())
    assert _envelopes.tool_reply(big) == big


def test_full_replies_context_disables_the_net(monkeypatch):
    """The OMP projection spills full results itself, so it must receive them whole."""
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "100")
    big = json.dumps(_big())
    with _envelopes.full_replies():
        assert _envelopes.tool_reply(big) == big
    assert _envelopes.tool_reply(big) != big


def test_existing_truncated_field_is_not_clobbered(monkeypatch):
    monkeypatch.setenv(_envelopes.REPLY_BUDGET_ENV, "4000")
    out = json.loads(_envelopes.tool_reply(json.dumps(
        {"ok": True, "truncated": "tool's own note", "rows": list(range(5000))})))
    assert out["truncated"] == "tool's own note" and "reply_truncated" in out


# ---------------------------------------------------------------- tool-side compaction

def _pair(a: str, b: str, d: float, **extra) -> dict:
    return {"a": a, "b": b, "distance": d, **extra}


def test_interference_compaction_pages_clearances_tightest_first():
    rows = [_pair(f"p{i}", f"q{i}", 10.0 + i) for i in range(220)]
    rows.append(_pair("x", "y", 0.1, clearance_status="below"))
    res = {"complete": True, "interferences": [], "contacts": [], "clearances": rows,
           "parts": [{"name": f"p{i}"} for i in range(60)]}
    out = compact_interference(res)
    assert out["clearances_total"] == 221 and len(out["clearances"]) == 20
    assert out["clearances"][0]["a"] == "x"
    assert out["clearance_violations"] == [rows[-1]]
    assert out["clearances_page"]["next_offset"] == 20
    assert len(out["parts"]) == 30 and out["parts_total"] == 60
    second = compact_interference(res, clearance_offset=20, include_parts=False)
    assert second["clearances"][0]["distance"] == 29.0 and "parts" not in second
    small = {"clearances": [_pair("a", "b", 3.0)], "parts": [{"name": "a"}]}
    kept = compact_interference(small)
    assert kept["clearances"] == small["clearances"] and kept["parts"] == small["parts"]


def test_touching_pair_becomes_a_contact_and_is_named():
    res = {"clearances": [_pair("A", "B", 0.0), _pair("A", "C", 2.0),
                          _pair("B", "C", 0.0, approx=True)], "contacts": []}
    promote_touching(res)
    assert [(c["a"], c["b"], c["touching"]) for c in res["contacts"]] == [("A", "B", True)]
    assert len(res["clearances"]) == 2
    assert "A<->B" in touching_note(res) and "contacts" in touching_note(res)
    kept = {"clearances": [_pair("A", "B", 0.0)], "contacts": []}
    assert "see clearances" in touching_note(kept)


def test_mass_bodies_paged_heaviest_first_with_totals():
    bodies = [{"body_id": i + 1, "mass_g": float(i), "volume_mm3": 1.0, "com": [0, 0, 0],
               "inertia": [0] * 9, "principal_axes": [[1, 0, 0]] * 3,
               "material": {"density_g_cm3": 7.85, "source": "default"},
               "closed_solid": False, "warning": "open shell"} for i in range(650)]
    mass = {"mass_g": 1.0, "body_count": 650, "bodies": bodies}
    out = _compact_mass_bodies(mass, 20, 0)
    assert len(out["bodies"]) == 20 and out["bodies"][0]["body_id"] == 650
    assert "inertia" not in out["bodies"][0]
    assert out["bodies_page"]["next_offset"] == 20 and out["bodies_page"]["total"] == 650
    assert out["bodies_by_material"][0]["bodies"] == 650
    assert out["open_body_count"] == 650
    assert _compact_mass_bodies({"bodies": bodies[:3]}, 20, 0)["bodies"] == bodies[:3]
    warnings = _mass_warnings(mass)
    assert len(warnings) <= 3 and any(w.startswith("650 bodies (") for w in warnings)


def test_density_without_registry_basis_is_flagged(monkeypatch):
    from services.mcp import cad_eval_tools
    monkeypatch.setattr(cad_eval_tools, "_material_registry_density",
                        lambda key: 2.7 if key == "al6061" else None)
    known = {"material": {"key": "al6061", "density_g_cm3": 2.7, "source": "document_material"}}
    assert _mass_warnings(known) == [] and known["material"]["density_known"] is True
    visual = {"material": {"key": "chrome_paint", "density_g_cm3": 7.85,
                           "source": "document_material"}}
    warnings = _mass_warnings(visual)
    assert visual["material"]["density_known"] is False
    assert len(warnings) == 1 and "FALLBACK" in warnings[0] and "chrome_paint" in warnings[0]
    explicit = {"material": {"density_g_cm3": 1.0, "source": "explicit"}}
    assert _mass_warnings(explicit) == [] and "density_known" not in explicit["material"]
