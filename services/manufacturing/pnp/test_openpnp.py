from pathlib import Path
import pytest

from .model import (
    Board, CalibrationRecord, Feeder, Fiducial, Nozzle, Package, Panel,
    PanelInstance, Placement, PnpJob,
)
from .openpnp import (
    MAX_ELEMENTS,
    MAX_XML_BYTES,
    OpenPnpRefusal,
    export_openpnp_job,
    import_openpnp_job,
)

FIXTURES = Path(__file__).parents[3] / "fixtures" / "mfg" / "pnp"


def test_imports_real_openpnp_shaped_job_fixture() -> None:
    job = import_openpnp_job(FIXTURES / "demo.job.xml")
    assert job.id == "demo-job"
    assert job.board.name == "Controller"
    assert [(p.ref_des, p.x_mm, p.side, p.enabled) for p in job.board.placements] == [
        ("R1", 10.25, "top", True),
        ("R2", 30.0, "bottom", False),
    ]
    assert job.panel is not None
    assert [(i.id, i.x_mm) for i in job.panel.instances] == [("panel-0", 0.0), ("panel-1", 110.0)]


def test_openpnp_job_round_trip_fidelity(tmp_path: Path) -> None:
    board = Board(
        "board-7", "Sensor", "C", 42.5, 18.25,
        placements=(
            Placement("U1", "U1", "MCU", "QFN32", 5.125, 6.25, -45.0),
            Placement("C1", "C1", "CAP", "0402", 7.5, 8.75, 90.0, "bottom", False),
        ),
    )
    job = PnpJob(
        "job-7", board,
        packages=(
            Package("QFN32", "QFN-32", 5.0, 5.0, 0.9, ("N2",), (0.1, -0.2), 2.5),
            Package("0402", "0402", 1.0, 0.5, 0.35, ("N1",)),
        ),
        feeders=(Feeder("F1", "MCU", x_mm=100.0, y_mm=25.0, remaining_count=12),),
        nozzles=(Nozzle("N2", 1.0, 10.0, ("QFN32",)),),
        calibrations=(
            CalibrationRecord(
                "CAL7", "N2", "nozzle_offset", "virtual-grid",
                "2026-08-08T00:00:00Z", uncertainty_mm=0.02,
                evidence_refs=("res:virtual:1",),
            ),
        ),
        panel=Panel(
            "panel-7", "board-7",
            (
                PanelInstance("b0", 0.0, 0.0),
                PanelInstance("b1", 45.0, 0.0, 180.0, False),
            ),
            (Fiducial("PF1", 2.0, 2.0, scope="panel"),),
        ),
    )
    path = export_openpnp_job(job, tmp_path / "sensor.job.xml")
    assert path.read_text(encoding="utf-8").startswith("<?xml")
    assert "<openpnp-job" in path.read_text(encoding="utf-8")
    assert "<openpnp-board" in (tmp_path / "board-7.board.xml").read_text(encoding="utf-8")
    restored = import_openpnp_job(path)
    assert restored == job



def test_openpnp_forbids_doctype_and_entities(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile.job.xml"
    hostile.write_text(
        '<!DOCTYPE job [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<openpnp-job><board-locations><board-location board_file="board.xml">'
        '<location x="0" y="0"/></board-location></board-locations>&xxe;</openpnp-job>',
        encoding="utf-8",
    )
    with pytest.raises(OpenPnpRefusal) as caught:
        import_openpnp_job(hostile)
    assert caught.value.code == "openpnp_job_declaration"
    assert "/etc/passwd" not in caught.value.reason


def test_openpnp_refuses_board_traversal_and_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.board.xml"
    outside.write_text('<openpnp-board id="x"><dimensions x="1" y="1"/></openpnp-board>', encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    traversal = nested / "traversal.job.xml"
    traversal.write_text(
        '<openpnp-job><board-locations>'
        '<board-location id="x" board_file="../outside.board.xml"/>'
        '</board-locations></openpnp-job>',
        encoding="utf-8",
    )
    with pytest.raises(OpenPnpRefusal) as traversal_error:
        import_openpnp_job(traversal)
    assert traversal_error.value.code == "openpnp_board_traversal"

    board_link = nested / "linked.board.xml"
    board_link.symlink_to(outside)
    linked = nested / "linked.job.xml"
    linked.write_text(
        '<openpnp-job><board-locations>'
        '<board-location id="x" board_file="linked.board.xml"/>'
        '</board-locations></openpnp-job>',
        encoding="utf-8",
    )
    with pytest.raises(OpenPnpRefusal) as symlink_error:
        import_openpnp_job(linked)
    assert symlink_error.value.code == "openpnp_board_symlink"


def test_openpnp_refuses_oversized_and_excessive_element_xml(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.job.xml"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_XML_BYTES + 1)
    with pytest.raises(OpenPnpRefusal) as size_error:
        import_openpnp_job(oversized)
    assert size_error.value.code == "openpnp_job_too_large"

    excessive = tmp_path / "elements.job.xml"
    excessive.write_text(
        "<openpnp-job>" + "<x/>" * MAX_ELEMENTS + "</openpnp-job>",
        encoding="utf-8",
    )
    with pytest.raises(OpenPnpRefusal) as element_error:
        import_openpnp_job(excessive)
    assert element_error.value.code == "openpnp_job_element_limit"


def test_openpnp_export_refuses_symlink_target(tmp_path: Path) -> None:
    protected = tmp_path / "protected.xml"
    protected.write_text("keep", encoding="utf-8")
    output = tmp_path / "sensor.job.xml"
    output.symlink_to(protected)
    board = Board("board", "Board", "1", 1.0, 1.0)
    with pytest.raises(OpenPnpRefusal) as caught:
        export_openpnp_job(PnpJob("job", board, ()), output)
    assert caught.value.code == "openpnp_output_symlink"
    assert protected.read_text(encoding="utf-8") == "keep"


def test_openpnp_export_refuses_excessive_element_work(tmp_path: Path) -> None:
    placement = Placement("R1", "R1", "RES", "0402", 0.0, 0.0, 0.0)
    board = Board(
        "board",
        "Board",
        "1",
        1.0,
        1.0,
        placements=(placement,) * (MAX_ELEMENTS // 3 + 1),
    )
    with pytest.raises(OpenPnpRefusal) as caught:
        export_openpnp_job(PnpJob("job", board, ()), tmp_path / "job.xml")
    assert caught.value.code == "openpnp_export_element_limit"