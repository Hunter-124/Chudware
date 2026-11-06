"""Readable stock identities and retained search terms."""

import pytest

from services.parts.adapters.extrusion_adapter import ExtrusionAdapter
from services.parts.adapters.orings_adapter import OringsAdapter
from services.parts.adapters.raw_stock_adapter import RawStockAdapter


@pytest.mark.parametrize(
    "adapter,part_id,title,family,alias",
    [
        (OringsAdapter(), "seal:AS568-014:nbr",
         'AS568-014 O-Ring 0.489" ID x 0.070" CS, Buna-N (NBR)',
         "AS568 O-Ring", "nitrile"),
        (ExtrusionAdapter(), "section:T-SLOT:2020",
         "2020 T-Slot Aluminum Extrusion 20mm x 20mm, 6105-T5",
         "T-Slot Aluminum Extrusion", "8020"),
        (RawStockAdapter(), "section:ROUND-BAR:6061-T6:0.25in",
         '0.25" dia round bar Aluminum 6061-T6', "Round Bar", "barstock"),
    ],
)
def test_stock_identity_and_direct_alias_search(adapter, part_id, title, family, alias):
    record = adapter.get(part_id)
    assert record["title"] == title
    assert record["family"] == family
    assert alias not in record["title"].lower()
    assert any(alias in value.lower() for value in record["aliases"])
    assert record in adapter.search(alias)


@pytest.mark.parametrize("adapter", [OringsAdapter(), ExtrusionAdapter(), RawStockAdapter()])
def test_every_stock_record_has_family_and_aliases(adapter):
    for record in adapter.iter_records():
        assert record["family"]
        assert record["aliases"]
