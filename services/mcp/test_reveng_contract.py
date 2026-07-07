"""RE-00 contract tests — validates reveng DAG templates + embedded JSON examples.

Run: pytest services/mcp/test_reveng_contract.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.dag.graph_loader import validate_graph, GraphValidationError

_TEMPLATES = (
    "reveng_scan.json",
    "reveng_datasheet.json",
    "reveng_single_photo.json",
)

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "dag" / "templates"

# Embedded examples from contracts/reveng.md §2–§5
_SESSION_EXAMPLE = {
    "schema": "aicad.reveng_session/1",
    "session_id": "re_8f3c2a1b",
    "name": "M8 bracket from photos",
    "created_at": "2026-07-07T15:30:00Z",
    "updated_at": "2026-07-07T16:12:00Z",
    "status": "capturing",
    "part_ref": "catalog:grabcad:bracket-123",
    "stages": {
        "capture": "done",
        "scale": "pending",
        "reconstruct": "idle",
        "model": "idle",
        "review": "idle",
        "deliver": "idle",
    },
    "scale": {
        "method": "aruco_mat",
        "mm_per_unit": 1.0,
        "residual_pct": 0.4,
        "confirmed": False,
    },
    "artifacts": {
        "images": 12,
        "sparse_dir": "sparse/0",
        "mesh_path": None,
        "splat_path": None,
        "doc_id": None,
    },
    "honesty": {
        "pipeline": "scan_mesh_ai_parametric",
        "single_photo_tier": None,
        "approximate": False,
    },
    "provenance": [],
}

_FITS_EXAMPLE = {
    "schema": "aicad.reveng_fits/1",
    "session_id": "re_8f3c2a1b",
    "mesh_path": "mesh/cleaned.ply",
    "fits": [
        {
            "kind": "cylinder",
            "confidence": 0.92,
            "params_mm": {
                "radius": 4.0,
                "height": 12.0,
                "axis": [0, 0, 1],
                "origin": [0, 0, 0],
            },
            "source": "ransac",
            "face_ids": ["f12", "f13"],
        },
    ],
    "sections": [],
}

_EVENT_EXAMPLE = {
    "schema": "aicad.reveng_events/1",
    "run_id": "run_a1b2c3d4",
    "session_id": "re_8f3c2a1b",
    "stage": "reconstruct",
    "pct": 45,
    "msg": "Sparse reconstruction: 18/24 images registered",
    "artifact": "sparse/0",
    "ts": "2026-07-07T15:45:00Z",
}

_DEVIATION_EXAMPLE = {
    "ok": True,
    "session_id": "re_8f3c2a1b",
    "max_mm": 0.35,
    "rms_mm": 0.12,
    "buckets": [
        {"range_mm": [0.0, 0.1], "face_count": 120, "color": "#00ff00"},
    ],
    "mesh_path": "mesh/cleaned.ply",
    "brep_doc_id": "doc_abc123",
}


def _validate_session(doc: dict) -> None:
    assert doc.get("schema") == "aicad.reveng_session/1"
    assert doc.get("session_id", "").startswith("re_")
    assert doc.get("status") in {
        "capturing", "reconstructing", "modeling", "review", "done", "error",
    }
    for key in ("stages", "scale", "artifacts", "honesty", "provenance"):
        assert key in doc, f"missing session field {key!r}"


def _validate_fits(doc: dict) -> None:
    assert doc.get("schema") == "aicad.reveng_fits/1"
    assert isinstance(doc.get("fits"), list)
    for fit in doc["fits"]:
        assert fit.get("kind") in {"box", "cylinder", "sphere", "cone", "plane"}
        assert "params_mm" in fit


def _validate_event(doc: dict) -> None:
    assert doc.get("schema") == "aicad.reveng_events/1"
    assert 0 <= int(doc.get("pct", -1)) <= 100
    assert doc.get("stage")


def _validate_deviation(doc: dict) -> None:
    assert doc.get("ok") is True
    assert "max_mm" in doc and "rms_mm" in doc
    assert isinstance(doc.get("buckets"), list)


@pytest.mark.parametrize("template_file", _TEMPLATES)
def test_reveng_template_passes_validate_graph(template_file: str) -> None:
    path = _TEMPLATE_DIR / template_file
    assert path.exists(), f"Template not found: {path}"
    graph = json.loads(path.read_text(encoding="utf-8"))
    validate_graph(graph)


@pytest.mark.parametrize("template_file", _TEMPLATES)
def test_reveng_template_all_nodes_have_end_condition(template_file: str) -> None:
    path = _TEMPLATE_DIR / template_file
    graph = json.loads(path.read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        ec = node.get("end_condition")
        assert ec and str(ec).strip(), (
            f"Template {template_file!r}: node {node['id']!r} missing end_condition"
        )


@pytest.mark.parametrize("template_file", _TEMPLATES)
def test_reveng_template_has_retry_back_edge(template_file: str) -> None:
    path = _TEMPLATE_DIR / template_file
    graph = json.loads(path.read_text(encoding="utf-8"))
    back_edges = [e for e in graph["edges"] if "retry_budget" in e]
    assert back_edges, f"Template {template_file!r} has no back-edges with retry_budget"


def test_reveng_template_ids_match_contract() -> None:
    expected = {
        "reveng_scan.json": "tmpl-reveng-scan",
        "reveng_datasheet.json": "tmpl-reveng-datasheet",
        "reveng_single_photo.json": "tmpl-reveng-single-photo",
    }
    for fname, tid in expected.items():
        graph = json.loads((_TEMPLATE_DIR / fname).read_text(encoding="utf-8"))
        assert graph["id"] == tid


@pytest.mark.parametrize(
    "validator,example",
    [
        (_validate_session, _SESSION_EXAMPLE),
        (_validate_fits, _FITS_EXAMPLE),
        (_validate_event, _EVENT_EXAMPLE),
        (_validate_deviation, _DEVIATION_EXAMPLE),
    ],
)
def test_embedded_contract_examples(validator, example) -> None:
    validator(example)


def test_contract_file_exists() -> None:
    path = Path(__file__).resolve().parents[2] / "contracts" / "reveng.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    for tool in (
        "reveng_start_session",
        "reveng_reconstruct",
        "reveng_build_parametric",
        "reveng_attach_result",
        "reveng_status",
    ):
        assert tool in text, f"contracts/reveng.md missing tool {tool!r}"


def test_routing_roles_present() -> None:
    routing = Path(__file__).resolve().parents[2] / "services" / "agent" / "routing.yaml"
    text = routing.read_text(encoding="utf-8")
    for role in ("re_modeler", "re_datasheet_reader", "re_geometry_verifier"):
        assert role in text, f"routing.yaml missing role {role!r}"


def test_invalid_graph_rejected() -> None:
    with pytest.raises(GraphValidationError):
        validate_graph({"schema": "aicad.dag/1", "id": "x", "nodes": [], "edges": []})
