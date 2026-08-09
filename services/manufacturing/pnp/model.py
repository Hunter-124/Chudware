"""Canonical, hardware-free pick-and-place records.

Lengths are millimetres and rotations are counter-clockwise degrees.  These
records deliberately contain no controller or transport handle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Package:
    id: str
    name: str
    width_mm: float
    length_mm: float
    height_mm: float
    compatible_nozzle_ids: tuple[str, ...] = ()
    pick_offset_mm: tuple[float, float] = (0.0, 0.0)
    pick_rotation_deg: float = 0.0


@dataclass(frozen=True)
class Placement:
    id: str
    ref_des: str
    part_id: str
    package_id: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    side: str = "top"
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.side not in {"top", "bottom"}:
            raise ValueError("placement side must be 'top' or 'bottom'")


@dataclass(frozen=True)
class Fiducial:
    id: str
    x_mm: float
    y_mm: float
    side: str = "top"
    diameter_mm: float = 1.0
    scope: str = "board"


@dataclass(frozen=True)
class CalibrationRecord:
    id: str
    subject_id: str
    kind: str
    method: str
    performed_at: str
    source: str = "simulated"
    uncertainty_mm: float | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Feeder:
    id: str
    part_id: str
    kind: str = "tape"
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    pitch_mm: float = 4.0
    remaining_count: int | None = None
    enabled: bool = True
    actuation: str = "manual"

    def __post_init__(self) -> None:
        if self.actuation != "manual":
            raise ValueError("PnP feeders are file/simulation resources; actuation must be manual")


@dataclass(frozen=True)
class Nozzle:
    id: str
    min_part_mm: float
    max_part_mm: float
    compatible_package_ids: tuple[str, ...] = ()

    def accepts(self, package: Package) -> bool:
        package_span = min(package.width_mm, package.length_mm)
        return (
            self.min_part_mm <= package_span <= self.max_part_mm
            and (not self.compatible_package_ids or package.id in self.compatible_package_ids)
            and (not package.compatible_nozzle_ids or self.id in package.compatible_nozzle_ids)
        )


@dataclass(frozen=True)
class Board:
    id: str
    name: str
    revision: str
    width_mm: float
    height_mm: float
    placements: tuple[Placement, ...] = ()
    fiducials: tuple[Fiducial, ...] = ()


@dataclass(frozen=True)
class PanelInstance:
    id: str
    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0
    enabled: bool = True


@dataclass(frozen=True)
class Panel:
    id: str
    board_id: str
    instances: tuple[PanelInstance, ...]
    fiducials: tuple[Fiducial, ...] = ()


@dataclass(frozen=True)
class PnpJob:
    id: str
    board: Board
    packages: tuple[Package, ...]
    feeders: tuple[Feeder, ...] = ()
    nozzles: tuple[Nozzle, ...] = ()
    calibrations: tuple[CalibrationRecord, ...] = ()
    panel: Panel | None = None

    def package_map(self) -> dict[str, Package]:
        return {item.id: item for item in self.packages}

    def feeder_map(self) -> dict[str, Feeder]:
        return {item.part_id: item for item in self.feeders if item.enabled}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def placement_count(job: PnpJob) -> int:
    copies = 1 if job.panel is None else sum(instance.enabled for instance in job.panel.instances)
    return len(job.board.placements) * copies
