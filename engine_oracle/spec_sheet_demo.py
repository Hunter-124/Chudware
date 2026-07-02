"""Workstream-B integration demo — soil-moisture sensor: priced BOM + JLCPCB fab quote (T-B5).

Day-5 integration point that wires the whole sourcing chain end to end for the sprint's
soil-moisture sensor demo:

1. A capacitor is matched through the **AI spec-sheet path** (:mod:`engine.spec_sheet_matcher`):
   a pasted datasheet -> extracted dims -> parametric build -> a ``VendorPart`` with
   ``source="spec_sheet"``. (The demo injects a mock LLM + a stub geometry builder so it runs
   fully offline and deterministically; the production tools use the real provider + core.)
2. A small assembly BOM is rolled up (:func:`engine.bom.rollup`): a made enclosure, the
   spec-sheet-matched capacitor (bought), and a linked PCB board expanded to its electronic BOM.
3. Prices are resolved (:func:`engine.bom.resolve_prices`) through an injectable provider chain,
   populating supplier links + unit/extended costs — an unpriceable line stays *unknown*, never
   guessed.
4. A JLCPCB PCB-fab quote is requested (:func:`engine.quote_providers.request_quote`). With no
   ``JLCPCB_KEY`` it degrades to the ``aicad.sourcing/1`` §0 shape (no network) — the demo shows
   the wired path either way.

Every network/core seam is injectable, so :func:`run_demo` is exercised offline in CI and, on a
provisioned host, runs against the real providers. Run:

    python -m engine.spec_sheet_demo
"""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from engine import bom, quote_providers
from engine import spec_sheet_matcher as ssm
from engine import vendor_parts


@contextlib.contextmanager
def _isolated_registry():
    """Redirect the vendor-parts cache + index to a throwaway temp dir for the duration.

    The demo registers a real ``source="spec_sheet"`` VendorPart; without isolation that would
    persist into the user's live ``%APPDATA%/AI-CAD/vendor_parts`` index. Swapping the two module
    constants keeps the demo (and its CI test) hermetic — nothing leaks into the real registry.
    """
    root = Path(tempfile.mkdtemp(prefix="spec_demo_registry_"))
    saved_root = vendor_parts.paths.VENDOR_PARTS
    saved_index = vendor_parts.INDEX
    vendor_parts.paths.VENDOR_PARTS = root
    vendor_parts.INDEX = root / "index.json"
    try:
        yield
    finally:
        vendor_parts.paths.VENDOR_PARTS = saved_root
        vendor_parts.INDEX = saved_index


def _mock_price_record(supplier: str, mpn: str, unit_price: float, url: str) -> dict:
    """A minimal ``aicad.sourcing/1`` §1 priced VendorPart record for the demo chain."""
    return {
        "supplier": supplier, "part_number": mpn, "mpn": mpn,
        "description": f"{mpn} (demo)", "product_url": url,
        "unit_price": unit_price, "currency": "USD",
        "price_breaks": [{"qty": 1, "unit_price": unit_price},
                         {"qty": 10, "unit_price": round(unit_price * 0.85, 4)}],
        "stock": 5000, "source": "demo",
    }


# --- a mock LLM that "reads" a capacitor datasheet -------------------------------------------

_DEMO_CAP_DATASHEET = """
Panasonic EEU-FR1V101  Aluminium Electrolytic Capacitor
Capacitance: 100 uF   Rated voltage: 35 V DC   Tolerance: +/- 20 %
Case size (D x L): 6.3 mm x 11.2 mm   Lead spacing: 2.5 mm   Lead dia: 0.6 mm
Radial, can type. RoHS.
"""


class _DemoCapProvider:
    """Deterministic mock provider returning the dims a real LLM would extract from the sheet."""

    name = "demo-cap"

    def complete(self, system: str, messages: list, max_tokens: int = 8000) -> str:  # noqa: D401
        return (
            '{"body_diameter_mm": 6.3, "body_length_mm": 11.2, "lead_spacing_mm": 2.5, '
            '"lead_diameter_mm": 0.6, "capacitance_uf": 100, "voltage_v": 35, '
            '"tolerance_pct": 20, "mpn": "EEU-FR1V101", '
            '"description": "100uF 35V radial electrolytic"}'
        )

    def complete_ex(self, system: str, messages: list, max_tokens: int = 8000):
        return self.complete(system, messages, max_tokens=max_tokens), ""


def _stub_build_model(template: str, dims: dict) -> Optional[str]:
    """Offline stand-in for the core geometry build: write a placeholder STEP so the demo can
    register a real spec-sheet ``VendorPart`` without the native core. On a provisioned host, pass
    ``build_model=None`` to :func:`run_demo` to use the real ``doc_apply_ops`` build instead."""
    out = Path(tempfile.mkdtemp(prefix="spec_demo_")) / f"{template}.step"
    out.write_text("ISO-10303-21;\n/* demo placeholder STEP */\nEND-ISO-10303-21;\n",
                   encoding="utf-8")
    return str(out)


# --- the demo assembly (soil-moisture sensor) ------------------------------------------------

def _demo_assembly(cap_mpn: str) -> dict:
    """An ``aicad.asm/1``-shaped soil-moisture sensor assembly tree (in-memory fixture).

    Enclosure = made; the spec-sheet capacitor = bought (matched via the AI path); the PCB is a
    linked board component expanded into its electronic BOM by the roll-up.
    """
    return {"tree": {"components": [
        {"name": "Sensor enclosure", "quantity": 1,
         "source": {"path": "enclosure.step"}},
        {"name": "Bulk capacitor", "quantity": 2, "supplier": "spec_sheet",
         "mpn": cap_mpn, "source": {"path": "cap.step"}},
        {"name": "Sensor PCB", "quantity": 1,
         "source": {"path": "sensor.kicad_pcb"}},
    ]}}


def _demo_board() -> dict:
    """A tiny ECAD board doc so the roll-up produces electronic BOM lines (probe FET + resistor)."""
    return {"footprints": [
        {"ref": "Q1", "fields": {"MPN": "BSS138"}, "value": "BSS138"},
        {"ref": "R1", "fields": {"MPN": "RC0805FR-0710KL"}, "value": "10k"},
        {"ref": "R2", "fields": {"MPN": "RC0805FR-0710KL"}, "value": "10k"},
    ]}


# --- pricing chain (mock; a real host uses the Nexar->DigiKey->Mouser chain) ------------------

class _DemoChain:
    """Injectable pricing chain: returns a priced record (with a supplier link) for known MPNs.

    ``spec_sheet_mpns`` are parts already matched through the AI spec-sheet path: their priced
    record keeps ``source="spec_sheet"`` so provenance survives pricing (a distributor price + link
    is attached to a spec-sheet-matched part, not a wholesale replacement of its provenance).
    """

    _PRICES = {
        "EEU-FR1V101": ("Digi-Key", 0.42, "https://www.digikey.com/short/eeufr1v101"),
        "BSS138": ("Digi-Key", 0.11, "https://www.digikey.com/short/bss138"),
        "RC0805FR-0710KL": ("Digi-Key", 0.02, "https://www.digikey.com/short/rc0805-10k"),
    }

    def __init__(self, spec_sheet_mpns: Optional[set] = None) -> None:
        self._spec = {str(m).strip() for m in (spec_sheet_mpns or set())}

    def fetch(self, mpn: str) -> Optional[dict]:
        key = str(mpn).strip()
        hit = self._PRICES.get(key)
        if not hit:
            return None
        supplier, price, url = hit
        rec = _mock_price_record(supplier, mpn, price, url)
        if key in self._spec:
            rec["source"] = "spec_sheet"
        return rec


def run_demo(*, build_model: Optional[Callable[[str, dict], Optional[str]]] = _stub_build_model,
             provider: Any = None,
             chain: Any = None,
             quote_fn: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    """Run the soil-moisture sensor sourcing demo end to end, returning a structured result.

    All seams are injectable (defaulting to offline stubs) so this runs in CI with no core, no keys
    and no network. On a provisioned host pass ``build_model=None`` / ``provider=None`` /
    ``chain=None`` to exercise the real core, LLM and provider chain.

    Returns ``{spec_sheet, bom, priced_count, spec_sheet_lines, supplier_links, jlcpcb_quote}``.
    """
    # 1. AI spec-sheet match the capacitor -> a source="spec_sheet" VendorPart.
    prov = provider if provider is not None else _DemoCapProvider()
    extracted = ssm.ai_extract_dims(_DEMO_CAP_DATASHEET, "capacitor_radial", provider=prov)
    cap_mpn = extracted["mpn"] or "EEU-FR1V101"
    with _isolated_registry():
        spec = ssm.register_from_spec_sheet(
            extracted["dims"], "capacitor_radial", cap_mpn,
            description=extracted["description"], build_model=build_model)

    # 2. Roll up the assembly BOM (spec-sheet cap resolves to its registered VendorPart).
    def _vendor_lookup(comp: dict) -> Optional[dict]:
        if str(comp.get("mpn") or "").strip() == cap_mpn:
            return spec.get("vendor_part")
        return None

    lines = bom.rollup(
        "sensor.asm",
        load_assembly=lambda _p: _demo_assembly(cap_mpn),
        load_board=lambda _p: _demo_board(),
        vendor_lookup=_vendor_lookup,
    )

    # 3. Resolve prices (supplier links + costs) through the injectable chain. The default chain
    #    keeps the spec-sheet cap's provenance while attaching a distributor price + link.
    bom.resolve_prices(lines, chain=chain if chain is not None else _DemoChain({cap_mpn}))

    # 4. Request the JLCPCB PCB-fab quote (degrades to §0 offline with no JLCPCB_KEY).
    quote = (quote_fn or quote_providers.request_quote)(
        _stub_gerber_zip(), "jlcpcb", quantity=5, layers=2)

    priced = [ln for ln in lines if ln.unit_cost is not None]
    spec_sheet_lines = [
        ln for ln in lines
        if (ln.vendor_part or {}).get("source") == "spec_sheet"
    ]
    supplier_links = sorted({
        (ln.vendor_part or {}).get("product_url", "")
        for ln in lines if (ln.vendor_part or {}).get("product_url")
    })
    return {
        "spec_sheet": spec,
        "bom": bom.to_dicts(lines),
        "priced_count": len(priced),
        "spec_sheet_lines": bom.to_dicts(spec_sheet_lines),
        "supplier_links": supplier_links,
        "jlcpcb_quote": quote,
    }


def _stub_gerber_zip() -> str:
    """A placeholder Gerber bundle path for the quote request (offline; no key -> no upload)."""
    out = Path(tempfile.mkdtemp(prefix="spec_demo_gerber_")) / "sensor_gerbers.zip"
    out.write_bytes(b"PK\x03\x04demo")  # minimal ZIP magic; never uploaded without a key
    return str(out)


def _fmt_price(v: Optional[float]) -> str:
    return f"${v:,.2f}" if v is not None else "  (unpriced)"


def main() -> None:
    res = run_demo()
    print("=" * 70)
    print("Soil-moisture sensor — sourcing integration demo (T-B5)")
    print("=" * 70)
    spec = res["spec_sheet"]
    print(f"\nAI spec-sheet match: {spec['mpn']}  registered={spec['registered']}")
    print(spec["preview"])
    print("\nPriced BOM:")
    for ln in res["bom"]:
        cost = _fmt_price(ln.get("ext_cost"))
        src = (ln.get("vendor_part") or {}).get("source", ln.get("kind"))
        print(f"  [{ln['kind']:6}] {ln['description'][:38]:38} x{ln['quantity']:<3} "
              f"{cost:>12}  ({src})")
    print(f"\nspec-sheet lines: {len(res['spec_sheet_lines'])}   "
          f"priced lines: {res['priced_count']}")
    print("supplier links:")
    for link in res["supplier_links"]:
        print(f"  - {link}")
    q = res["jlcpcb_quote"]
    print(f"\nJLCPCB fab quote: ok={q.get('ok')}  " +
          (f"total={_fmt_price(q.get('total_price'))}" if q.get("ok")
           else f"reason={q.get('reason')}"))


if __name__ == "__main__":
    main()
