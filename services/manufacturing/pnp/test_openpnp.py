from pathlib import Path

from .model import (
    Board, CalibrationRecord, Feeder, Fiducial, Nozzle, Package, Panel,
    PanelInstance, Placement, PnpJob,
)
from .openpnp import export_openpnp_job, import_openpnp_job

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
