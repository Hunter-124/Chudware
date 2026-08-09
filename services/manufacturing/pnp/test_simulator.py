import pytest

from .capability import capability_report
from .model import (
    Board, CalibrationRecord, Feeder, Nozzle, Package, Panel, PanelInstance,
    Placement, PnpJob,
)
from .simulator import PnpResourceRefusal, VirtualPnpMachine


def _job() -> PnpJob:
    return PnpJob(
        id="run-accounting",
        board=Board(
            "board", "Board", "A", 50.0, 30.0,
            placements=(
                Placement("R1", "R1", "RES", "0402", 1.0, 2.0, 0.0),
                Placement("R2", "R2", "RES", "0402", 3.0, 4.0, 0.0, enabled=False),
                Placement("R3", "R3", "RES", "0402", 5.0, 6.0, 0.0),
            ),
        ),
        packages=(Package("0402", "0402", 1.0, 0.5, 0.35, ("N1",)),),
        feeders=(Feeder("F1", "RES", remaining_count=100),),
        nozzles=(Nozzle("N1", 0.1, 2.0, ("0402",)),),
        calibrations=(CalibrationRecord("CAL1", "N1", "nozzle_offset", "virtual", "2026-01-01T00:00:00Z"),),
        panel=Panel("panel", "board", (PanelInstance("P0", 0.0, 0.0), PanelInstance("P1", 55.0, 0.0))),
    )


def test_virtual_run_accounts_for_every_placement_outcome() -> None:
    run = VirtualPnpMachine().run(_job(), fault_placement_ids={"R3"})
    assert len(run.outcomes) == 6
    assert (run.evidence.placed, run.evidence.skipped, run.evidence.faulted) == (2, 2, 2)
    assert run.evidence.total == 6
    assert run.evidence.source == "simulated"
    assert run.evidence.advisory_only is True
    assert run.evidence.satisfies_execute_gate is False
    assert run.evidence.deterministic_digest == VirtualPnpMachine().run(_job(), fault_placement_ids={"R3"}).evidence.deterministic_digest


def test_virtual_run_refuses_cartesian_outcome_blowup() -> None:
    base = _job()
    placement = base.board.placements[0]
    instance = PanelInstance("copy", 0.0, 0.0)
    oversized = PnpJob(
        base.id,
        Board(
            base.board.id,
            base.board.name,
            base.board.revision,
            base.board.width_mm,
            base.board.height_mm,
            placements=(placement,) * 1_001,
        ),
        base.packages,
        base.feeders,
        base.nozzles,
        base.calibrations,
        Panel("large", base.board.id, (instance,) * 1_000),
    )
    with pytest.raises(PnpResourceRefusal) as caught:
        VirtualPnpMachine().run(oversized)
    assert caught.value.code == "pnp_simulation_limit"


def test_pnp_capability_is_capped_and_execute_side_is_unsupported() -> None:
    report = capability_report()
    levels = {item.level: item for item in report["levels"]}
    assert report["max_advertised"] == "simulate"
    assert {name for name, item in levels.items() if item.truth == "verified"} == {"plan", "generate", "simulate"}
    for name in ("transport", "execute", "safety_integration"):
        assert levels[name].truth == "unsupported"
        assert levels[name].why
