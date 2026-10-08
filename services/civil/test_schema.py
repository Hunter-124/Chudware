"""Structural cases from contracts/civil_schema.md §3 and network default regressions."""
from copy import deepcopy
import pytest
from . import schema
from .errors import CivilError


def model_fixture():
    model = schema.new_model("Test site", {"horizontal": "EPSG:26915"})
    model["water_networks"] = [schema.water_network("w", junctions=[
        {"id": "j", "x": 500000, "y": 100, "elevation": 3, "demand_m3s": .01}],
        reservoirs=[{"id": "r", "x": 500000, "y": 0, "head": 30}],
        pipes=[{"id": "p", "from": "r", "to": "j", "length": 100,
                "diameter": .2, "roughness": 120, "vertices": []}])]
    model["gravity_networks"] = [schema.gravity_network("g", nodes=[
        {"id": "a", "type": "manhole", "x": 0, "y": 0, "rim": 4, "invert": 1},
        {"id": "b", "type": "outfall", "x": 100, "y": 0, "rim": 3, "invert": 0}],
        conduits=[{"id": "c", "from": "a", "to": "b", "shape": "circular",
                   "diameter": .3, "length": None, "n": .013}])]
    model["catchments"] = [{"id": "lot", "name": "Lot", "outlet": "a", "area_m2": 100,
                            "runoff_coeff": .5, "cn": 80, "imperv_pct": 40,
                            "polygon": None, "tc_s": None, "tc_segments": []}]
    model["alignments"] = [{"id": "road", "name": "Road", "start_station": 0,
                            "horizontal": [{"type": "line", "start": [0, 0], "end": [10, 0]},
                                           {"type": "line", "start": [10, 0], "end": [20, 0]}], "vertical": None}]
    model["surfaces"] = [{"id": "eg", "name": "EG", "kind": "tin",
                          "points": [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
                          "triangles": [[0, 1, 2]], "boundary": None, "breaklines": []}]
    model["tie_ins"] = [{"id": "tie", "civil_network": "w", "civil_node": "j",
                         "piping_doc_id": "doc", "piping_study": "piping_network",
                         "piping_endpoint": {"run_id": "R1", "end": "start"},
                         "direction": "supply", "elevation_offset_m": 0}]
    return model


def test_complete_fixture_is_structurally_valid():
    assert schema.validate(model_fixture()) == []


@pytest.mark.parametrize("collection,field,value,expected", [
    ("water_networks", "pipes", {"to": "absent"}, ".to: dangling"),
    ("water_networks", "pipes", {"length": 0}, ".length: must"),
    ("gravity_networks", "conduits", {"diameter": -.3}, ".diameter: must"),
    ("gravity_networks", "conduits", {"from": "j"}, ".from: dangling"),
])
def test_link_dimensions_and_refs(collection, field, value, expected):
    model = model_fixture()
    model[collection][0][field][0].update(value)
    assert any(expected in error for error in schema.validate(model))


def test_duplicate_ids_and_cross_collection_node_namespace():
    model = model_fixture()
    model["surfaces"].append(deepcopy(model["surfaces"][0]))
    model["water_networks"][0]["reservoirs"][0]["id"] = "j"
    errors = schema.validate(model)
    assert any("surfaces[1].id: duplicate" in e for e in errors)
    assert any("duplicate node id across" in e for e in errors)


def test_catchment_and_tiein_refs_are_validated_in_scope():
    model = model_fixture()
    model["catchments"][0]["outlet"] = "j"  # Exists in water, not gravity.
    model["tie_ins"][0]["civil_node"] = "a"  # Exists in gravity, not network w.
    errors = schema.validate(model)
    assert any("catchments[0].outlet: dangling" in e for e in errors)
    assert any("tie_ins[0].civil_node: dangling" in e for e in errors)


def test_alignment_continuity_tolerance_and_tin_indices():
    model = model_fixture()
    model["alignments"][0]["horizontal"][1]["start"][0] += 5e-7
    assert schema.validate(model) == []
    model["alignments"][0]["horizontal"][1]["start"][0] += 2e-6
    model["surfaces"][0]["triangles"] = [[0, 1, 3], [0, 2, 1]]
    errors = schema.validate(model)
    assert any("discontinuous alignment" in e for e in errors)
    assert any("triangle indices must index points" in e for e in errors)
    assert any("triangle must be nondegenerate and CCW" in e for e in errors)


def test_spiral_endpoint_controls_following_element_contiguity():
    # Zero-curvature clothoid integrates to a straight 10 m tangent (analytic).
    model = schema.new_model("Spiral")
    model["alignments"] = [{"id": "a", "name": "A", "start_station": 0, "vertical": None,
        "horizontal": [{"type": "spiral", "start": [1, 2], "start_heading": 0,
                        "length": 10, "radius_start": None, "radius_end": None, "dir": "ccw"},
                       {"type": "line", "start": [11, 2], "end": [12, 2]}]}]
    assert schema.validate(model) == []
    model["alignments"][0]["horizontal"][1]["start"] = [11, 3]
    assert any("discontinuous alignment" in e for e in schema.validate(model))


def test_required_fields_and_nonfinite_coordinates():
    model = model_fixture()
    del model["catchments"][0]["area_m2"]
    model["water_networks"][0]["junctions"][0]["x"] = float("nan")
    errors = schema.validate(model)
    assert any(".area_m2: required" in e for e in errors)
    assert any("junctions[0].x: must be finite" in e for e in errors)


@pytest.mark.parametrize("collection,child,field", [
    ("catchments", None, "area_m2"),
    ("water_networks", "junctions", "x"),
    ("water_networks", "reservoirs", "y"),
    ("water_networks", "pipes", "diameter"),
    ("gravity_networks", "nodes", "type"),
    ("gravity_networks", "conduits", "diameter"),
    ("surfaces", None, "points"),
    ("alignments", None, "start_station"),
    ("tie_ins", None, "civil_node"),
])
def test_required_fields_refuse_null(collection, child, field):
    model = model_fixture()
    element = model[collection][0]
    if child is not None:
        element = element[child][0]
    element[field] = None
    assert any(f".{field}:" in error for error in schema.validate(model))


def test_catchment_optional_fields_may_be_absent_without_mutation():
    model = model_fixture()
    model["catchments"] = [{"id": "minimal", "outlet": "a", "area_m2": 100}]
    original = deepcopy(model)
    assert schema.validate(model) == []
    assert model == original


def test_null_optional_catchment_fields_equal_absent_without_mutation():
    model = model_fixture()
    model["catchments"] = [{"id": "minimal", "outlet": "a", "area_m2": 100,
                            **{field: None for field in ("name", "runoff_coeff", "cn",
                               "imperv_pct", "polygon", "tc_s", "tc_segments")}}]
    original = deepcopy(model)
    assert schema.validate(model) == []
    assert model == original


@pytest.mark.parametrize("field,value", [
    ("name", 4), ("tc_s", 0), ("tc_s", "60"), ("tc_s", float("nan")),
    ("tc_segments", {}), ("tc_segments", [3]),
    ("runoff_coeff", -.01), ("runoff_coeff", 1.01),
    ("cn", 29.99), ("cn", 100.01), ("cn", True),
    ("imperv_pct", -.01), ("imperv_pct", 100.01), ("polygon", "invalid"),
])
def test_present_catchment_optional_fields_must_be_well_typed(field, value):
    model = model_fixture()
    model["catchments"] = [{"id": "minimal", "outlet": "a", "area_m2": 100, field: value}]
    assert any(f"catchments[0].{field}:" in error for error in schema.validate(model))


@pytest.mark.parametrize("cn", [30, 100])
def test_catchment_optional_field_valid_boundaries(cn):
    model = model_fixture()
    model["catchments"] = [{"id": "minimal", "outlet": "a", "area_m2": 100,
                            "cn": cn, "tc_s": None, "tc_segments": [{}],
                            "runoff_coeff": 0, "imperv_pct": 100, "polygon": None}]
    assert schema.validate(model) == []


def test_other_collections_accept_absent_optional_names_and_nullable_fields():
    model = model_fixture()
    for collection in ("surfaces", "alignments", "water_networks", "gravity_networks"):
        del model[collection][0]["name"]
    del model["alignments"][0]["vertical"]
    del model["water_networks"][0]["patterns"]
    del model["water_networks"][0]["options"]
    del model["water_networks"][0]["headloss"]
    del model["water_networks"][0]["junctions"][0]["demand_m3s"]
    del model["water_networks"][0]["pipes"][0]["roughness"]
    del model["gravity_networks"][0]["conduits"][0]["n"]
    del model["tie_ins"][0]["piping_study"]
    del model["tie_ins"][0]["elevation_offset_m"]
    assert schema.validate(model) == []
    model["water_networks"][0]["options"] = []
    model["alignments"][0]["vertical"] = []
    model["tie_ins"][0]["elevation_offset_m"] = "zero"
    errors = schema.validate(model)
    assert any("water_networks[0].options: must be an object" in e for e in errors)
    assert any("alignments[0].vertical: must contain pvis array" in e for e in errors)
    assert any("tie_ins[0].elevation_offset_m: must be finite" in e for e in errors)


def test_other_collection_null_optional_fields_equal_absent():
    model = model_fixture()
    for collection in ("surfaces", "alignments", "water_networks", "gravity_networks"):
        model[collection][0]["name"] = None
    model["surfaces"][0].update(boundary=None, breaklines=None)
    model["alignments"][0]["vertical"] = None
    model["water_networks"][0].update(patterns=None, options=None, headloss=None)
    model["water_networks"][0]["junctions"][0]["demand_m3s"] = None
    model["water_networks"][0]["pipes"][0].update(roughness=None, vertices=None)
    model["gravity_networks"][0]["conduits"][0].update(n=None, length=None, vertices=None)
    model["tie_ins"][0].update(piping_study=None, elevation_offset_m=None)
    model["structures"] = [{"id": "culvert", "kind": "culvert", "name": None}]
    original = deepcopy(model)
    assert schema.validate(model) == []
    assert model == original


def test_spiral_nullable_radii_may_be_omitted():
    model = schema.new_model("Spiral")
    model["alignments"] = [{"id": "a", "start_station": 0, "horizontal": [
        {"type": "spiral", "start": [0, 0], "start_heading": 0, "length": 10, "dir": "ccw"},
        {"type": "line", "start": [10, 0], "end": [20, 0]}]}]
    assert schema.validate(model) == []
    model["alignments"][0]["horizontal"][0]["radius_start"] = "infinite"
    assert any(".radius_start:" in e for e in schema.validate(model))


def test_invalid_collection_element_types_return_errors_not_exceptions():
    model = model_fixture()
    model["gravity_networks"][0]["conduits"][0]["shape"] = []
    model["structures"] = [{"id": "s", "kind": {}}]
    errors = schema.validate(model)
    assert any(".shape: invalid conduit shape" in e for e in errors)
    assert any(".kind: invalid structure kind" in e for e in errors)


def test_crud_copy_isolation_and_exact_refusals():
    model = schema.new_model("A")
    element = {"id": "a", "kind": "culvert"}
    schema.upsert(model, "structures", element)
    element["kind"] = "changed"
    assert schema.find(model, "structures", "a")["kind"] == "culvert"
    schema.upsert(model, "structures", {"id": "a", "kind": "thrust_block"})
    assert len(model["structures"]) == 1
    schema.remove(model, "structures", "a")
    with pytest.raises(CivilError) as exc:
        schema.find(model, "structures", "a")
    assert exc.value.reason == "not_found"
    with pytest.raises(CivilError) as exc:
        schema.upsert(model, "unknown", {"id": "x"})
    assert exc.value.reason == "unknown_collection"


@pytest.mark.parametrize("null_collections", [False, True])
def test_minimal_water_network_validates_and_solves(null_collections):
    from . import water
    model = schema.new_model("Minimal water")
    network = {
        "id": "w",
        "junctions": [{"id": "j", "x": 100, "y": 0, "elevation": 3, "demand_m3s": .01}],
        "reservoirs": [{"id": "r", "x": 0, "y": 0, "head": 30}],
        "pipes": [{"id": "p", "from": "r", "to": "j", "length": 100, "diameter": .2, "roughness": 120}],
    }
    if null_collections:
        network.update(tanks=None, pumps=None, valves=None, patterns=None, options=None, curves=None)
    model["water_networks"] = [network]
    original = deepcopy(model)
    assert schema.validate(model) == []
    result = water.solve(network)
    assert result["converged"]
    assert result["timesteps"][0]["links"]["p"]["flow_m3s"] == pytest.approx(.01)
    assert model == original


@pytest.mark.parametrize("null_collections", [False, True])
def test_minimal_gravity_network_without_conduits_analyzes(null_collections):
    from . import gravity
    model = schema.new_model("Minimal gravity")
    network = {"id": "g", "kind": "storm", "nodes": [
        {"id": "o", "type": "outfall", "x": 0, "y": 0, "rim": 3, "invert": 0}]}
    if null_collections:
        network["conduits"] = None
    model["gravity_networks"] = [network]
    original = deepcopy(model)
    assert schema.validate(model) == []
    result = gravity.analyze(network)
    assert result["conduits"] == {}
    assert "o" in result["nodes"]
    assert model == original


@pytest.mark.parametrize("water", [False, True])
@pytest.mark.parametrize("null_collections", [False, True])
def test_empty_network_collections_are_structurally_optional(water, null_collections):
    model = schema.new_model("Empty network")
    keys = ("junctions", "reservoirs", "tanks", "pipes", "pumps", "valves", "patterns", "options", "curves") if water else ("nodes", "conduits")
    network = {"id": "n"} if water else {"id": "n", "kind": "storm"}
    if null_collections:
        network.update(dict.fromkeys(keys))
    model["water_networks" if water else "gravity_networks"] = [network]
    assert schema.validate(model) == []


def test_engines_refuse_empty_networks_with_clear_messages():
    from . import gravity, water
    for solve, network, message in (
        (water.solve, {"id": "w"}, "reservoir or tank"),
        (water.solve, {"id": "w", "reservoirs": [{"id": "r", "head": 30}]}, "pipe, pump, or valve"),
        (gravity.analyze, {"id": "g"}, "at least one node"),
    ):
        with pytest.raises(CivilError, match=message):
            solve(network)
