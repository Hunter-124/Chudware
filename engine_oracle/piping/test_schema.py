"""Schema tests for aicad.piping/1 (contracts/piping_schema.md). House style: main() + asserts.

Pure-Python (no kernel / fluids needed).

Run (from repo root):  python -m engine.piping.test_schema
"""

from __future__ import annotations

from engine.piping import schema


def test_factories_valid():
    doc = schema.new_piping("Line 100")
    assert doc["schema"] == schema.SCHEMA_PIPING
    r = schema.new_pipe_run(schema.next_id(doc, "r"),
                            path=[[0, 0, 0], [100, 0, 0], [100, 80, 0]], nps=2)
    doc["runs"].append(r)
    ft = schema.new_fitting(schema.next_id(doc, "ft"), "elbow", nps=2,
                            origin=(100, 0, 0), params={"angle": 90, "radius": "LR"})
    doc["fittings"].append(ft)
    assert schema.validate_piping(doc) == [], schema.validate_piping(doc)


def test_next_id_no_reuse():
    doc = schema.new_piping()
    a = schema.next_id(doc, "r")
    doc["runs"].append({"id": a})
    b = schema.next_id(doc, "r")
    assert (a, b) == ("r1", "r2")


def test_run_validation_errors():
    # too-few path points
    errs = schema.validate_run({"path": [[0, 0, 0]], "nps": 2})
    assert any("path" in e for e in errs)
    # zero-length segment
    errs = schema.validate_run({"path": [[0, 0, 0], [0, 0, 0]], "nps": 2})
    assert any("zero-length" in e for e in errs)
    # missing size
    errs = schema.validate_run({"path": [[0, 0, 0], [1, 0, 0]]})
    assert any("nps" in e for e in errs)
    # bad schedule
    errs = schema.validate_run({"path": [[0, 0, 0], [1, 0, 0]], "nps": 2, "schedule": "999X"})
    assert any("schedule" in e for e in errs)
    # good run is clean
    assert schema.validate_run(
        {"path": [[0, 0, 0], [1, 0, 0]], "nps": 2, "schedule": "40"}) == []


def test_fitting_validation_errors():
    # reducer requires nps2
    errs = schema.validate_fitting(
        {"type": "reducer", "nps": 2, "origin": [0, 0, 0], "orient": [0, 0, 0]})
    assert any("nps2" in e for e in errs)
    # bad elbow angle / radius
    errs = schema.validate_fitting(
        {"type": "elbow", "nps": 2, "origin": [0, 0, 0], "orient": [0, 0, 0],
         "params": {"angle": 30, "radius": "ZZ"}})
    assert any("angle" in e for e in errs) and any("radius" in e for e in errs)
    # bad flange class/face
    errs = schema.validate_fitting(
        {"type": "flange", "nps": 2, "origin": [0, 0, 0], "orient": [0, 0, 0],
         "params": {"flange_class": 7, "flange_face": "XX"}})
    assert any("flange_class" in e for e in errs) and any("flange_face" in e for e in errs)
    # unknown type
    errs = schema.validate_fitting(
        {"type": "wye", "nps": 2, "origin": [0, 0, 0], "orient": [0, 0, 0]})
    assert any("type" in e for e in errs)
    # good elbow is clean
    assert schema.validate_fitting(
        {"type": "elbow", "nps": 2, "origin": [0, 0, 0], "orient": [0, 0, 0],
         "params": {"angle": 45, "radius": "SR"}}) == []


def test_doc_validation_dupe_ids_and_schema():
    assert schema.validate_piping({"schema": "wrong"}) != []
    doc = schema.new_piping()
    doc["runs"] = [{"id": "r1", "path": [[0, 0, 0], [1, 0, 0]], "nps": 2},
                   {"id": "r1", "path": [[0, 0, 0], [1, 0, 0]], "nps": 2}]
    assert any("duplicate" in e for e in schema.validate_piping(doc))


def main():
    test_factories_valid()
    test_next_id_no_reuse()
    test_run_validation_errors()
    test_fitting_validation_errors()
    test_doc_validation_dupe_ids_and_schema()
    print("test_schema OK")


if __name__ == "__main__":
    main()
