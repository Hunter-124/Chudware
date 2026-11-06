"""Artifact content, archive safety and selection at the import boundary."""
from __future__ import annotations

import struct
import zipfile

import pytest

from services.browser import cad_validate

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
STL = b"solid part\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid part\n"


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)


@pytest.mark.parametrize(("name", "data"), [
    ("part.step", STEP),
    ("part.igs", b" " * 72 + b"S" + b"      1" + b"\n"),
    ("part.stl", STL),
    ("binary.stl", b"binary" + b"\0" * 74 + struct.pack("<I", 1) + b"\0" * 50),
    ("part.obj", b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"),
])
def test_supported_cad_headers(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    assert cad_validate.validate_cad(path)["ok"]
    assert cad_validate.classify_artifact(path)["cad_validated"] is True


@pytest.mark.parametrize("data", [b"", b"<html>login</html>", b"%PDF-1.7\n", b"not a STEP", b"ISO-10303-21;garbage"])
def test_invalid_step_is_not_cad(tmp_path, data):
    path = tmp_path / "part.step"
    path.write_bytes(data)
    assert not cad_validate.validate_cad(path)["ok"]


def test_pdf_is_a_document_even_with_step_extension(tmp_path):
    path = tmp_path / "wrong.step"
    path.write_bytes(b"%PDF-1.7\ncontent")
    assert cad_validate.classify_artifact(path)["artifact_kind"] == "document"
    assert not cad_validate.classify_artifact(path, "cad")["ok"]


def test_zip_chooses_requested_valid_member_and_extracts_safely(tmp_path):
    path = tmp_path / "models.zip"
    _zip(path, {"parts/mesh.stl": STL, "parts/model.step": STEP})
    ambiguous = cad_validate.validate_cad(path)
    assert not ambiguous["ok"] and set(ambiguous["candidates"]) == {"parts/model.step", "parts/mesh.stl"}
    assert cad_validate.validate_cad(path, requested_format="step")["member"] == "parts/model.step"
    assert cad_validate.validate_cad(path, requested_format="stl")["member"] == "parts/mesh.stl"
    classified = cad_validate.classify_artifact(path, "cad", requested_member="parts/model.step")
    assert classified["ok"] and classified["member"] == "parts/model.step"
    assert not cad_validate.classify_artifact(path, "cad", requested_member="missing.step")["ok"]
    assert not cad_validate.validate_cad(path, requested_format="iges")["ok"]
    out = cad_validate.extract_cad(path, requested_format="step")
    assert out["ok"] and out["archive_path"] == str(path)
    from pathlib import Path
    assert Path(out["artifact_path"]).read_bytes() == STEP
    assert Path(out["artifact_path"]).suffix == ".step"


def test_non_cad_and_unsafe_archives_do_not_become_geometry(tmp_path, monkeypatch):
    path = tmp_path / "model.zip"
    _zip(path, {"schematic.kicad_sym": "(kicad_symbol_lib)"})
    assert not cad_validate.validate_cad(path)["ok"]
    assert cad_validate.classify_artifact(path)["artifact_kind"] == "ecad"
    assert not cad_validate.classify_artifact(path, "cad")["ok"]
    _zip(path, {"../escape.step": STEP})
    assert not cad_validate.classify_artifact(path)["ok"]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("part.step", STEP)
        with pytest.warns(UserWarning):
            archive.writestr("part.step", b"<html>not CAD</html>")
    assert not cad_validate.classify_artifact(path)["ok"]
    _zip(path, {"a.step": STEP, "b.step": STEP})
    monkeypatch.setattr(cad_validate, "MAX_ZIP_MEMBERS", 1)
    assert not cad_validate.validate_cad(path)["ok"]


def test_3mf_and_freecad_require_model_structure(tmp_path):
    model = tmp_path / "model.3mf"
    _zip(model, {"[Content_Types].xml": "<Types/>", "3D/3dmodel.model": "<model unit='millimeter'/>"})
    assert cad_validate.validate_cad(model)["ok"]
    _zip(model, {"[Content_Types].xml": "<Types/>", "3D/3dmodel.model": "not XML"})
    assert not cad_validate.validate_cad(model)["ok"]
    fcstd = tmp_path / "part.FCStd"
    _zip(fcstd, {"Document.xml": '<Document SchemaVersion="4"><Objects Count="0"/></Document>'})
    assert cad_validate.validate_cad(fcstd)["format"] == "fcstd"
    _zip(fcstd, {"Document.xml": "<not-freecad/>"})
    assert not cad_validate.validate_cad(fcstd)["ok"]


def test_proprietary_cad_is_never_marked_validated(tmp_path):
    path = tmp_path / "part.sldprt"
    path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"data")
    assert not cad_validate.validate_cad(path)["ok"]
    out = cad_validate.classify_artifact(path, "cad")
    assert out["ok"] and out["cad_validated"] is False
    path.write_bytes(b"<html>login</html>")
    assert not cad_validate.classify_artifact(path, "cad")["ok"]


def test_ambiguous_archive_requires_explicit_member(tmp_path):
    path = tmp_path / "variants.zip"
    _zip(path, {"first.step": STEP, "second.step": STEP})
    result = cad_validate.validate_cad(path)
    assert not result["ok"] and result["candidates"] == ["first.step", "second.step"]
    classified = cad_validate.classify_artifact(path)
    assert classified["artifact_kind"] == "archive" and classified["candidates"] == result["candidates"]
    selected = cad_validate.extract_cad(path, requested_member="second.step")
    assert selected["ok"] and selected["member"] == "second.step"


def test_same_named_formats_prefer_step(tmp_path):
    path = tmp_path / "same-model.zip"
    _zip(path, {"part.stl": STL, "part.step": STEP})
    result = cad_validate.validate_cad(path)
    assert result["ok"] and result["format"] == "step" and result["member"] == "part.step"
