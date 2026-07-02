"""Offline test for the workstream-B integration demo (T-B5).

Drives :func:`engine.spec_sheet_demo.run_demo` with its default offline stubs (mock LLM, stub
geometry builder, mock pricing chain, no JLCPCB key) and asserts the DoD: a priced BOM for the
soil-moisture sensor with at least one spec-sheet-matched part, supplier links, and a JLCPCB fab
quote that degrades to the contract shape offline. No network, no core, no keys.
"""
from __future__ import annotations

from engine import spec_sheet_demo as demo


def test_demo_runs_offline_and_prices_bom():
    res = demo.run_demo()
    # A spec-sheet-matched capacitor was registered.
    assert res["spec_sheet"]["registered"] is True
    assert res["spec_sheet"]["source"] == "spec_sheet"

    # The BOM has at least one line sourced from the spec-sheet path.
    assert len(res["spec_sheet_lines"]) >= 1
    assert all((ln.get("vendor_part") or {}).get("source") == "spec_sheet"
               for ln in res["spec_sheet_lines"])

    # Priced: the mock chain populated unit/ext costs for the known MPNs.
    assert res["priced_count"] >= 1
    priced = [ln for ln in res["bom"] if ln.get("ext_cost") is not None]
    assert priced and all(ln["ext_cost"] >= 0 for ln in priced)

    # Supplier links surfaced from the priced records.
    assert res["supplier_links"]
    assert all(link.startswith("http") for link in res["supplier_links"])


def test_demo_spec_sheet_line_is_priced_with_quantity():
    res = demo.run_demo()
    line = res["spec_sheet_lines"][0]
    assert line["quantity"] == 2            # the assembly uses two bulk caps
    assert line["unit_cost"] is not None    # resolved through the mock chain
    # ext = unit * qty (price-break aware; qty 2 uses the qty-1 break).
    assert abs(line["ext_cost"] - line["unit_cost"] * 2) < 1e-9


def test_demo_jlcpcb_quote_degrades_without_key():
    # With no JLCPCB_KEY the fab quote must degrade to the sourcing §0 shape, never a traceback.
    res = demo.run_demo()
    q = res["jlcpcb_quote"]
    assert isinstance(q, dict)
    assert q.get("ok") is False
    assert "reason" in q


def test_demo_does_not_pollute_real_registry():
    from engine import vendor_parts
    before = vendor_parts.INDEX
    demo.run_demo()
    # The isolated-registry context restored the module constants.
    assert vendor_parts.INDEX == before
    # And the real index has no spec-sheet demo part.
    if before.exists():
        assert "spec_sheet::eeu-fr1v101" not in before.read_text().lower()
