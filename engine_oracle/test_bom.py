"""BOM roll-up tests — fully offline (no DLL, no network, no API keys).

Covers: made/bought classification, instance aggregation, ECAD-board merge (via the real
`write_bom` CSV), price resolution from price-break tiers through a mocked provider chain, and
the contract-shaped "price unknown" degradation when a provider declines.
"""
from __future__ import annotations

from engine import bom
from engine.bom import BomLine


# --- Fixtures (in-code dicts, mirroring aicad.asm/1 + aicad.pcb/1 shapes) ----------------------

def _assembly() -> dict:
    """An assembly tree with: 1 made body, a bought fastener (x2 instances), and a linked board."""
    return {
        "kind": "aicad.asm/1",
        "components": [
            {"id": "c1", "name": "bracket", "source": {"path": "parts/bracket.step"}},
            {"id": "c2", "name": "M3x10 screw", "supplier": "McMaster-Carr",
             "part_number": "91251A542", "mpn": "91251A542"},
            {"id": "c3", "name": "M3x10 screw", "supplier": "McMaster-Carr",
             "part_number": "91251A542", "mpn": "91251A542"},
            {"id": "c4", "name": "control board", "source": {"path": "boards/ctrl.ecad+"}},
        ],
    }


def _board() -> dict:
    return {
        "kind": "aicad.pcb/1",
        "footprints": [
            {"id": "fp1", "ref": "R1", "value": "10k", "attr": "smd",
             "fpid": "Resistor_SMD:R_0603", "fields": {"MPN": "RC0603FR-0710KL"}},
            {"id": "fp2", "ref": "R2", "value": "10k", "attr": "smd",
             "fpid": "Resistor_SMD:R_0603", "fields": {"MPN": "RC0603FR-0710KL"}},
            {"id": "fp3", "ref": "C1", "value": "100nF", "attr": "smd",
             "fpid": "Capacitor_SMD:C_0603", "fields": {"MPN": "CL10B104KB8NNNC"}},
            {"id": "fp4", "ref": "TP1", "value": "TP", "attr": "virtual",
             "fpid": "TestPoint:TP", "fields": {}},  # DNP -> excluded from BOM
        ],
    }


class _FakeChain:
    """Mock ProviderChain: returns a priced §1 record for known MPNs, None otherwise."""

    def __init__(self, records: dict[str, dict]):
        self.records = records
        self.calls: list[str] = []

    def fetch(self, mpn: str):
        self.calls.append(mpn)
        return self.records.get(mpn)


def _record(supplier: str, part_number: str, breaks: list[dict], currency: str = "USD") -> dict:
    return {
        "supplier": supplier, "part_number": part_number, "description": part_number,
        "unit_price": breaks[0]["unit_price"] if breaks else None, "currency": currency,
        "price_breaks": breaks, "stock": 1000, "lead_time_days": 3, "moq": 1,
        "fetched_ts": 1735689600.0,
    }


def _rollup():
    return bom.rollup(
        "asm.step",
        load_assembly=lambda _p: _assembly(),
        load_board=lambda _p: _board(),
    )


# --- rollup: classification + aggregation + ECAD merge ----------------------------------------

def test_rollup_classifies_made_and_bought() -> None:
    lines = _rollup()
    made = [l for l in lines if l.kind == "made"]
    bought = [l for l in lines if l.kind == "bought"]
    # 1 made (bracket), 1 aggregated fastener (bought), 2 ECAD lines (R group + C; TP1 excluded).
    assert len(made) == 1
    assert made[0].description == "bracket"
    assert made[0].vendor_part is None and made[0].cost_model is None
    assert len(bought) == 3
    for l in bought:
        assert l.cost_model is None  # bought lines never carry a cost model


def test_bought_instances_aggregate_quantity() -> None:
    lines = _rollup()
    fastener = [l for l in lines if l.mpn == "91251A542"]
    assert len(fastener) == 1
    assert fastener[0].quantity == 2  # c2 + c3 collapsed
    assert fastener[0].part_ref == "mcmaster-carr::91251a542"


def test_ecad_board_merged() -> None:
    lines = _rollup()
    ecad = [l for l in lines if l.mpn in ("RC0603FR-0710KL", "CL10B104KB8NNNC")]
    assert {l.mpn for l in ecad} == {"RC0603FR-0710KL", "CL10B104KB8NNNC"}
    resistor = next(l for l in ecad if l.mpn == "RC0603FR-0710KL")
    assert resistor.quantity == 2  # R1 + R2 grouped by write_bom
    assert resistor.kind == "bought"
    # The virtual test point is excluded from the electronic BOM.
    assert not any("TP" in (l.description or "") for l in lines)


def test_rollup_no_board_when_unlinked() -> None:
    asm = {"components": [{"id": "c1", "name": "bracket", "source": {"path": "b.step"}}]}
    lines = bom.rollup("x", load_assembly=lambda _p: asm,
                       load_board=lambda _p: (_ for _ in ()).throw(AssertionError("no board")))
    assert len(lines) == 1 and lines[0].kind == "made"


def test_line_ids_unique() -> None:
    lines = _rollup()
    ids = [l.line_id for l in lines]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("L") for i in ids)


# --- resolve_prices: price-break resolution + unknown degradation -----------------------------

def test_resolve_prices_populates_bought_only() -> None:
    lines = _rollup()
    chain = _FakeChain({
        "91251A542": _record("McMaster-Carr", "91251A542",
                             [{"qty": 1, "unit_price": 0.42}, {"qty": 100, "unit_price": 0.31}]),
        "RC0603FR-0710KL": _record("Digi-Key", "RC0603FR-0710KL",
                                   [{"qty": 1, "unit_price": 0.10}]),
        "CL10B104KB8NNNC": _record("Mouser", "CL10B104KB8NNNC",
                                   [{"qty": 1, "unit_price": 0.05}]),
    })
    priced = bom.resolve_prices(lines, chain=chain)

    made = next(l for l in priced if l.kind == "made")
    assert made.vendor_part is None and made.unit_cost is None  # untouched by pricing

    fastener = next(l for l in priced if l.mpn == "91251A542")
    assert fastener.vendor_part is not None
    assert fastener.part_ref == "mcmaster-carr::91251a542"
    # qty 2 -> the qty>=1 tier (0.42) is the highest break with qty<=2 (100-tier does not apply).
    assert fastener.unit_cost == 0.42
    assert fastener.ext_cost == 0.84
    assert fastener.currency == "USD"


def test_price_break_tier_selected_for_quantity() -> None:
    line = BomLine(line_id="L1", kind="bought", quantity=150, mpn="X")
    chain = _FakeChain({"X": _record("S", "X",
                        [{"qty": 1, "unit_price": 0.42}, {"qty": 100, "unit_price": 0.31}])})
    bom.resolve_prices([line], chain=chain)
    assert line.unit_cost == 0.31  # 100-tier applies at qty 150
    assert line.ext_cost == 0.31 * 150


def test_unpriced_line_stays_unknown() -> None:
    """A provider that declines (returns None) leaves the line contract-shaped 'unknown'."""
    lines = _rollup()
    chain = _FakeChain({})  # every fetch returns None
    priced = bom.resolve_prices(lines, chain=chain)
    for l in priced:
        if l.kind == "bought":
            assert l.unit_cost is None and l.ext_cost is None  # never fabricated
            assert l.vendor_part is None
    # The chain WAS consulted for each bought MPN (no silent skip).
    assert set(chain.calls) == {"91251A542", "RC0603FR-0710KL", "CL10B104KB8NNNC"}


def test_ecad_line_without_mpn_not_fetched() -> None:
    board = {"footprints": [{"id": "f", "ref": "H1", "value": "hole", "attr": "smd",
                             "fpid": "Mount:H", "fields": {}}]}
    lines = bom.rollup("x", load_assembly=lambda _p: {"components": [
        {"id": "c", "name": "b", "source": {"path": "brd.ecad+"}}]},
        load_board=lambda _p: board)
    chain = _FakeChain({})
    bom.resolve_prices(lines, chain=chain)
    assert lines and lines[0].mpn is None
    assert lines[0].unit_cost is None
    assert chain.calls == []  # no MPN -> nothing to fetch


# --- serialization ----------------------------------------------------------------------------

def test_to_dicts_is_contract_shaped() -> None:
    lines = _rollup()
    dicts = bom.to_dicts(lines)
    required = {"line_id", "kind", "quantity", "unit", "part_ref", "vendor_part",
                "cost_model", "unit_cost", "ext_cost", "currency"}
    for d in dicts:
        assert required <= set(d)  # every frozen §3 field present
        assert d["kind"] in ("made", "bought")
        # exactly one of vendor_part / cost_model is non-null after pricing is a rollup invariant
        # for bought vs made; before pricing bought vendor_part may be None (unresolved) — allowed.


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
