import pytest

from services.parts.adapters.heatsinks_adapter import HeatsinksAdapter
from services.parts.adapters.hydraulic_components_adapter import HydraulicComponentsAdapter
from services.parts.adapters.piping_fittings_adapter import PipingFittingsAdapter
from services.parts.adapters.structural_sections_adapter import StructuralSectionsAdapter
from services.parts.index import PartsIndex


@pytest.mark.parametrize(
    "adapter,part_id,title,family,alias_query",
    [
        (HeatsinksAdapter(), "thermal:heatsink:HS-BGA-14",
         "BGA Push-Pin/Adhesive Heatsink 14×14×10 mm", "BGA Heatsink", "heat sink"),
        (PipingFittingsAdapter(), "fitting:elbow:NPS0.5",
         "ASME B16.9 Elbow NPS 0.5", "ASME B16.9 Elbow", "piping fitting"),
        (StructuralSectionsAdapter(), "section:AISC360:C6x13",
         "AISC Channel C6×13", "AISC Channel", "structural section"),
        (StructuralSectionsAdapter(), "section:NDS:2x4",
         "S4S Dressed Lumber 2×4", "S4S Dressed Lumber", "structural section"),
        (HydraulicComponentsAdapter(), "hydraulic:ISO1219:valve_relief",
         "Relief Valve (ISO 1219)", "Relief Valve", "pressure-limiting valve"),
    ],
)
def test_titles_families_and_alias_discovery(adapter, part_id, title, family, alias_query):
    record = adapter.get(part_id)
    assert record is not None
    assert record["title"] == title
    assert record["family"] == family
    assert alias_query.lower() not in title.lower()
    assert any(r["part_id"] == part_id for r in adapter.search(alias_query))

    index = PartsIndex()
    index.rebuild(iter([record]))
    assert any(r["part_id"] == part_id for r in index.search(alias_query))


def test_all_industry_records_have_families_and_searchable_aliases():
    for adapter in (HeatsinksAdapter(), PipingFittingsAdapter(),
                    StructuralSectionsAdapter(), HydraulicComponentsAdapter()):
        records = list(adapter.iter_records())
        assert records
        assert len({r["part_id"] for r in records}) == len(records)
        for record in records:
            assert record["family"]
            assert record["aliases"]
            assert any(r["part_id"] == record["part_id"]
                       for r in adapter.search(record["aliases"][0]))
