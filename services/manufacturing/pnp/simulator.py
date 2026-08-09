"""Deterministic NullDriver-style PnP simulation.

The simulator advances logical ticks only and has no transport, subprocess,
device discovery, or physical-actuation entry point.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from itertools import islice
import json
from typing import Iterable

from .model import Nozzle, Package, PnpJob, Placement

MAX_FAULT_IDS = 100_000
MAX_SIMULATION_OUTCOMES = 1_000_000


class PnpResourceRefusal(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class PlacementOutcome:
    placement_id: str
    panel_instance_id: str
    outcome: str
    reason: str
    tick: int


@dataclass(frozen=True)
class PnpEvidenceSummary:
    source: str
    evidence_class: str
    advisory_only: bool
    satisfies_execute_gate: bool
    total: int
    placed: int
    skipped: int
    faulted: int
    deterministic_digest: str


@dataclass(frozen=True)
class VirtualPnpRun:
    outcomes: tuple[PlacementOutcome, ...]
    evidence: PnpEvidenceSummary


class VirtualPnpMachine:
    """A pure logical placement sequencer, analogous to a NullDriver."""

    driver = "virtual_null"

    def run(self, job: PnpJob, *, fault_placement_ids: Iterable[str] = ()) -> VirtualPnpRun:
        fault_values = tuple(islice(fault_placement_ids, MAX_FAULT_IDS + 1))
        if len(fault_values) > MAX_FAULT_IDS:
            raise PnpResourceRefusal(
                "pnp_fault_id_limit",
                f"fault injection exceeds the {MAX_FAULT_IDS} item limit",
            )
        fault_ids = frozenset(fault_values)
        package_map = job.package_map()
        feeders = job.feeder_map()
        nozzles = job.nozzles
        instances = (
            tuple(instance for instance in job.panel.instances if instance.enabled)
            if job.panel is not None
            else ()
        )
        instance_ids = tuple(item.id for item in instances) or (job.board.id,)
        outcome_count = len(instance_ids) * len(job.board.placements)
        if outcome_count > MAX_SIMULATION_OUTCOMES:
            raise PnpResourceRefusal(
                "pnp_simulation_limit",
                f"simulation exceeds the {MAX_SIMULATION_OUTCOMES} outcome limit",
            )
        outcomes: list[PlacementOutcome] = []
        tick = 0
        for instance_id in instance_ids:
            for placement in job.board.placements:
                tick += 1
                outcome, reason = self._place(placement, package_map, feeders, nozzles, fault_ids)
                outcomes.append(PlacementOutcome(placement.id, instance_id, outcome, reason, tick))
        counts = {name: sum(item.outcome == name for item in outcomes) for name in ("placed", "skipped", "faulted")}
        encoded = json.dumps([asdict(item) for item in outcomes], sort_keys=True, separators=(",", ":")).encode()
        evidence = PnpEvidenceSummary(
            source="simulated",
            evidence_class="simulation_conclusion",
            advisory_only=True,
            satisfies_execute_gate=False,
            total=len(outcomes),
            placed=counts["placed"],
            skipped=counts["skipped"],
            faulted=counts["faulted"],
            deterministic_digest=hashlib.sha256(encoded).hexdigest(),
        )
        return VirtualPnpRun(tuple(outcomes), evidence)

    @staticmethod
    def _place(
        placement: Placement,
        packages: dict[str, Package],
        feeders: dict[str, object],
        nozzles: tuple[Nozzle, ...],
        fault_ids: frozenset[str],
    ) -> tuple[str, str]:
        if not placement.enabled:
            return "skipped", "placement_disabled"
        if placement.id in fault_ids:
            return "faulted", "injected_virtual_fault"
        package = packages.get(placement.package_id)
        if package is None:
            return "faulted", "package_missing"
        if placement.part_id not in feeders:
            return "faulted", "feeder_missing"
        if not any(nozzle.accepts(package) for nozzle in nozzles):
            return "faulted", "compatible_nozzle_missing"
        return "placed", "virtual_placement_completed"
