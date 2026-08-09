from __future__ import annotations

from dataclasses import replace
import pytest

from services.manufacturing.slicer.contract import (
    MAX_CANONICAL_BYTES,
    MAX_SLICE_COLLECTION_ITEMS,
    ContractValidationError,
    Determinism,
    ModelRef,
    ProfileRef,
    SliceRequest,
    canonical_json,
)
from services.manufacturing.slicer.profiles import (
    FilamentProfile,
    PrinterProfile,
    ProcessProfile,
)


def _profile_refs() -> tuple[ProfileRef, ...]:
    return (
        ProfileRef("printer", "printer-a", 2, "1" * 64),
        ProfileRef("filament", "pla", 4, "2" * 64),
        ProfileRef("process", "quality", 8, "3" * 64),
    )


def _models() -> tuple[ModelRef, ...]:
    return (
        ModelRef(
            "b",
            "/tmp/b.stl",
            "b" * 64,
            transform={"rotation": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
        ),
        ModelRef(
            "a",
            "/tmp/a.stl",
            "a" * 64,
            transform={"scale": [1.0, 1.0, 1.0], "rotation": [0.0, 0.0, 0.0]},
        ),
    )


def test_request_id_is_canonical_and_semantic() -> None:
    first = SliceRequest(
        project_id="project",
        project_rev=3,
        plate_id="plate",
        sidecar_build_id="build-9",
        profiles=_profile_refs(),
        models=_models(),
        determinism=Determinism(seed=91),
        overrides={"z": {"b": 2, "a": 1}, "a": True},
    )
    reordered = SliceRequest(
        project_id="project",
        project_rev=3,
        plate_id="plate",
        sidecar_build_id="build-9",
        profiles=tuple(reversed(_profile_refs())),
        models=tuple(reversed(_models())),
        determinism=Determinism(seed=91),
        overrides={"a": True, "z": {"a": 1, "b": 2}},
    )

    assert first.request_id == reordered.request_id
    assert canonical_json(first.identity_dict()) == canonical_json(reordered.identity_dict())
    assert replace(first, determinism=Determinism(seed=92)).request_id != first.request_id
    assert replace(first, project_rev=4).request_id != first.request_id


def test_every_printer_profile_field_participates_in_hash() -> None:
    profile = PrinterProfile(
        id="p",
        name="Printer",
        rev=1,
        vendor="Vendor",
        model="Model",
        build_volume={"shape": "rect", "x": 200.0, "y": 200.0, "z": 200.0},
        extruders=({"index": 0, "nozzle_mm": 0.4},),
        bed={"heated": True, "max_temp_c": 110},
        chamber={"heated": False, "max_temp_c": 0},
        gcode_flavor="marlin2",
        kinematics="cartesian",
        raw_bundle={"machine_limits_usage": "time_estimate_only"},
    )
    changes = {
        "id": "p2",
        "name": "Printer 2",
        "rev": 2,
        "vendor": "Other",
        "model": "Other Model",
        "build_volume": {"shape": "rect", "x": 201.0, "y": 200.0, "z": 200.0},
        "extruders": ({"index": 0, "nozzle_mm": 0.6},),
        "bed": {"heated": True, "max_temp_c": 111},
        "chamber": {"heated": True, "max_temp_c": 50},
        "gcode_flavor": "klipper",
        "kinematics": "corexy",
        "supports_arcs": True,
        "supports_bgcode": True,
        "firmware_retraction": True,
        "inherits": "base",
        "raw_bundle": {"machine_limits_usage": "emit_to_gcode"},
    }
    for field_name, value in changes.items():
        assert replace(profile, **{field_name: value}).profile_hash != profile.profile_hash, field_name


def test_every_filament_and_process_field_participates_in_hash() -> None:
    filament = FilamentProfile(
        id="pla",
        name="PLA",
        rev=1,
        typed={"diameter_mm": 1.75, "density_g_cm3": 1.24},
        raw_bundle={"filament_colour": "#ffffff"},
    )
    filament_changes = {
        "id": "pla2",
        "name": "PLA 2",
        "rev": 2,
        "typed": {"diameter_mm": 1.76, "density_g_cm3": 1.24},
        "raw_bundle": {"filament_colour": "#000000"},
        "inherits": "generic-pla",
        "extruder_hint": 1,
    }
    for field_name, value in filament_changes.items():
        assert replace(filament, **{field_name: value}).profile_hash != filament.profile_hash, field_name

    process = ProcessProfile(
        id="quality",
        name="Quality",
        rev=1,
        typed={"layer_height_mm": 0.2, "wall_count": 3},
        raw_bundle={"avoid_crossing_perimeters": True},
    )
    process_changes = {
        "id": "draft",
        "name": "Draft",
        "rev": 2,
        "typed": {"layer_height_mm": 0.3, "wall_count": 3},
        "raw_bundle": {"avoid_crossing_perimeters": False},
        "inherits": "base-process",
    }
    for field_name, value in process_changes.items():
        assert replace(process, **{field_name: value}).profile_hash != process.profile_hash, field_name


def test_oversized_profile_payload_is_a_named_refusal() -> None:
    with pytest.raises(ContractValidationError, match="canonical_input_bytes"):
        ProcessProfile(
            id="hostile",
            name="Hostile",
            rev=1,
            typed={},
            raw_bundle={"payload": "x" * (MAX_CANONICAL_BYTES + 1)},
        )


def test_slice_request_refuses_oversized_model_collection_before_sorting() -> None:
    model = ModelRef("m", "/tmp/model.stl", "a" * 64)
    with pytest.raises(ContractValidationError, match="slice_collection_limit"):
        SliceRequest(
            project_id="p",
            project_rev=1,
            plate_id="plate",
            sidecar_build_id="build",
            profiles=_profile_refs(),
            models=(model,) * (MAX_SLICE_COLLECTION_ITEMS + 1),
            determinism=Determinism(1),
        )
