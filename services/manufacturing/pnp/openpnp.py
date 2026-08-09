"""OpenPnP-shaped board/job XML file interchange.

The interchange is intentionally limited to files.  It does not import OpenPnP
code, start a JVM, discover devices, or expose a driver.
"""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from .model import (
    Board, CalibrationRecord, Feeder, Fiducial, Nozzle, Package, Panel,
    PanelInstance, Placement, PnpJob,
)

_VERSION = "1.1"
_UNITS = "Millimeters"
_CHUDWARE_NS = "urn:chudware:pnp:1"
ET.register_namespace("chudware", _CHUDWARE_NS)


def _f(value: float) -> str:
    return format(value, ".15g")


def _location(parent: ET.Element, x: float, y: float, rotation: float = 0.0) -> None:
    ET.SubElement(parent, "location", units=_UNITS, x=_f(x), y=_f(y), z="0", rotation=_f(rotation))


def _write_xml(root: ET.Element, path: Path) -> None:
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True, short_empty_elements=True)


def export_openpnp_job(job: PnpJob, path: str | Path) -> Path:
    """Emit an OpenPnP ``.job.xml`` plus its referenced ``.board.xml``.

    Chudware-only metadata is stored in namespaced attributes/elements. OpenPnP
    readers ignore those while this importer uses them for lossless round trips.
    """
    job_path = Path(path)
    job_path.parent.mkdir(parents=True, exist_ok=True)
    board_path = job_path.with_name(f"{job.board.id}.board.xml")

    board_root = ET.Element(
        "openpnp-board",
        version=_VERSION,
        id=job.board.id,
        name=job.board.name,
        revision=job.board.revision,
    )
    ET.SubElement(board_root, "dimensions", units=_UNITS, x=_f(job.board.width_mm), y=_f(job.board.height_mm))
    packages = ET.SubElement(board_root, "packages")
    for package in job.packages:
        node = ET.SubElement(
            packages,
            "package",
            id=package.id,
            name=package.name,
            width=_f(package.width_mm),
            length=_f(package.length_mm),
            height=_f(package.height_mm),
            compatible_nozzles=",".join(package.compatible_nozzle_ids),
            pick_offset_x=_f(package.pick_offset_mm[0]),
            pick_offset_y=_f(package.pick_offset_mm[1]),
            pick_rotation=_f(package.pick_rotation_deg),
        )
        node.tail = None
    placements = ET.SubElement(board_root, "placements")
    for placement in job.board.placements:
        node = ET.SubElement(
            placements,
            "placement",
            id=placement.id,
            ref_des=placement.ref_des,
            side=placement.side.title(),
            type="Placement",
            enabled=str(placement.enabled).lower(),
            part_id=placement.part_id,
            package_id=placement.package_id,
        )
        _location(node, placement.x_mm, placement.y_mm, placement.rotation_deg)
        ET.SubElement(node, "part", id=placement.part_id)
    fiducials = ET.SubElement(board_root, "fiducials")
    for fiducial in job.board.fiducials:
        node = ET.SubElement(
            fiducials,
            "placement",
            id=fiducial.id,
            side=fiducial.side.title(),
            type="Fiducial",
            enabled="true",
            diameter=_f(fiducial.diameter_mm),
            scope=fiducial.scope,
        )
        _location(node, fiducial.x_mm, fiducial.y_mm)
    _write_xml(board_root, board_path)

    root = ET.Element("openpnp-job", version=_VERSION, id=job.id)
    locations = ET.SubElement(root, "board-locations")
    instances = (
        job.panel.instances
        if job.panel is not None
        else (PanelInstance(id=f"{job.board.id}-1", x_mm=0.0, y_mm=0.0),)
    )
    for instance in instances:
        node = ET.SubElement(
            locations,
            "board-location",
            id=instance.id,
            board_file=board_path.name,
            side="Top",
            enabled=str(instance.enabled).lower(),
            check_fiducials=str(bool(job.board.fiducials)).lower(),
        )
        _location(node, instance.x_mm, instance.y_mm, instance.rotation_deg)
    if job.panel is not None:
        ET.SubElement(root, "panel", id=job.panel.id, board_id=job.panel.board_id)
        panel_fiducials = ET.SubElement(root, f"{{{_CHUDWARE_NS}}}panel-fiducials")
        for fiducial in job.panel.fiducials:
            node = ET.SubElement(
                panel_fiducials,
                f"{{{_CHUDWARE_NS}}}fiducial",
                id=fiducial.id,
                side=fiducial.side,
                diameter=_f(fiducial.diameter_mm),
                scope=fiducial.scope,
            )
            _location(node, fiducial.x_mm, fiducial.y_mm)
    resources = ET.SubElement(root, f"{{{_CHUDWARE_NS}}}resources")
    for feeder in job.feeders:
        ET.SubElement(
            resources,
            f"{{{_CHUDWARE_NS}}}feeder",
            id=feeder.id,
            part_id=feeder.part_id,
            kind=feeder.kind,
            x=_f(feeder.x_mm),
            y=_f(feeder.y_mm),
            z=_f(feeder.z_mm),
            pitch=_f(feeder.pitch_mm),
            remaining="" if feeder.remaining_count is None else str(feeder.remaining_count),
            enabled=str(feeder.enabled).lower(),
            actuation=feeder.actuation,
        )
    for nozzle in job.nozzles:
        ET.SubElement(
            resources,
            f"{{{_CHUDWARE_NS}}}nozzle",
            id=nozzle.id,
            min_part=_f(nozzle.min_part_mm),
            max_part=_f(nozzle.max_part_mm),
            compatible_packages=",".join(nozzle.compatible_package_ids),
        )
    for calibration in job.calibrations:
        ET.SubElement(
            resources,
            f"{{{_CHUDWARE_NS}}}calibration",
            id=calibration.id,
            subject_id=calibration.subject_id,
            kind=calibration.kind,
            method=calibration.method,
            performed_at=calibration.performed_at,
            source=calibration.source,
            uncertainty="" if calibration.uncertainty_mm is None else _f(calibration.uncertainty_mm),
            evidence_refs=",".join(calibration.evidence_refs),
        )
    _write_xml(root, job_path)
    return job_path


def _required(node: ET.Element, name: str) -> str:
    value = node.get(name)
    if value is None:
        raise ValueError(f"OpenPnP XML missing required attribute {name!r} on <{node.tag}>")
    return value


def _float(node: ET.Element, name: str, default: float | None = None) -> float:
    value = node.get(name)
    if value is None:
        if default is None:
            raise ValueError(f"OpenPnP XML missing numeric attribute {name!r} on <{node.tag}>")
        return default
    return float(value)


def _parse_location(parent: ET.Element) -> tuple[float, float, float]:
    node = parent.find("location")
    if node is None:
        raise ValueError(f"OpenPnP XML missing <location> under <{parent.tag}>")
    units = node.get("units", _UNITS)
    scale = 25.4 if units.lower() in {"inches", "inch"} else 1.0
    return _float(node, "x") * scale, _float(node, "y") * scale, _float(node, "rotation", 0.0)


def import_openpnp_job(path: str | Path) -> PnpJob:
    """Parse an OpenPnP job and the board files it references."""
    job_path = Path(path)
    root = ET.parse(job_path).getroot()
    if root.tag != "openpnp-job":
        raise ValueError("expected OpenPnP <openpnp-job> root")
    board_locations = root.findall("./board-locations/board-location")
    if not board_locations:
        raise ValueError("OpenPnP job contains no board locations")
    board_file = _required(board_locations[0], "board_file")
    if any(_required(node, "board_file") != board_file for node in board_locations):
        raise ValueError("this offline interchange supports one board revision per job")
    board_root = ET.parse(job_path.parent / board_file).getroot()
    if board_root.tag != "openpnp-board":
        raise ValueError("referenced file is not an OpenPnP board")

    dimensions = board_root.find("dimensions")
    if dimensions is None:
        raise ValueError("OpenPnP board is missing dimensions")
    scale = 25.4 if dimensions.get("units", _UNITS).lower() in {"inches", "inch"} else 1.0
    packages = tuple(
        Package(
            id=_required(node, "id"),
            name=node.get("name", _required(node, "id")),
            width_mm=_float(node, "width") * scale,
            length_mm=_float(node, "length") * scale,
            height_mm=_float(node, "height") * scale,
            compatible_nozzle_ids=tuple(filter(None, node.get("compatible_nozzles", "").split(","))),
            pick_offset_mm=(_float(node, "pick_offset_x", 0.0) * scale, _float(node, "pick_offset_y", 0.0) * scale),
            pick_rotation_deg=_float(node, "pick_rotation", 0.0),
        )
        for node in board_root.findall("./packages/package")
    )
    placements: list[Placement] = []
    for node in board_root.findall("./placements/placement"):
        x, y, rotation = _parse_location(node)
        part_node = node.find("part")
        part_id = node.get("part_id") or (part_node.get("id") if part_node is not None else None)
        if not part_id:
            raise ValueError(f"placement {_required(node, 'id')} has no part id")
        placements.append(
            Placement(
                id=_required(node, "id"),
                ref_des=node.get("ref_des", _required(node, "id")),
                part_id=part_id,
                package_id=_required(node, "package_id"),
                x_mm=x,
                y_mm=y,
                rotation_deg=rotation,
                side=node.get("side", "Top").lower(),
                enabled=node.get("enabled", "true").lower() == "true",
            )
        )
    fiducials: list[Fiducial] = []
    for node in board_root.findall("./fiducials/placement"):
        x, y, _ = _parse_location(node)
        fiducials.append(
            Fiducial(
                id=_required(node, "id"),
                x_mm=x,
                y_mm=y,
                side=node.get("side", "Top").lower(),
                diameter_mm=_float(node, "diameter", 1.0),
                scope=node.get("scope", "board"),
            )
        )
    board = Board(
        id=_required(board_root, "id"),
        name=board_root.get("name", _required(board_root, "id")),
        revision=board_root.get("revision", ""),
        width_mm=_float(dimensions, "x") * scale,
        height_mm=_float(dimensions, "y") * scale,
        placements=tuple(placements),
        fiducials=tuple(fiducials),
    )
    instances = tuple(
        PanelInstance(
            id=_required(node, "id"),
            x_mm=_parse_location(node)[0],
            y_mm=_parse_location(node)[1],
            rotation_deg=_parse_location(node)[2],
            enabled=node.get("enabled", "true").lower() == "true",
        )
        for node in board_locations
    )
    panel_node = root.find("panel")
    panel = None
    if panel_node is not None or len(instances) != 1 or (instances[0].x_mm, instances[0].y_mm, instances[0].rotation_deg) != (0.0, 0.0, 0.0):
        panel_fiducials = []
        for node in root.findall(f"./{{{_CHUDWARE_NS}}}panel-fiducials/{{{_CHUDWARE_NS}}}fiducial"):
            x, y, _ = _parse_location(node)
            panel_fiducials.append(
                Fiducial(
                    id=_required(node, "id"),
                    x_mm=x,
                    y_mm=y,
                    side=node.get("side", "top"),
                    diameter_mm=_float(node, "diameter", 1.0),
                    scope=node.get("scope", "panel"),
                )
            )
        panel = Panel(
            id=panel_node.get("id", f"{board.id}-panel") if panel_node is not None else f"{board.id}-panel",
            board_id=board.id,
            instances=instances,
            fiducials=tuple(panel_fiducials),
        )
    resource_path = f"./{{{_CHUDWARE_NS}}}resources"
    feeders = tuple(
        Feeder(
            id=_required(node, "id"),
            part_id=_required(node, "part_id"),
            kind=node.get("kind", "tape"),
            x_mm=_float(node, "x", 0.0),
            y_mm=_float(node, "y", 0.0),
            z_mm=_float(node, "z", 0.0),
            pitch_mm=_float(node, "pitch", 4.0),
            remaining_count=int(node.attrib["remaining"]) if node.get("remaining") else None,
            enabled=node.get("enabled", "true").lower() == "true",
            actuation=node.get("actuation", "manual"),
        )
        for node in root.findall(resource_path + f"/{{{_CHUDWARE_NS}}}feeder")
    )
    nozzles = tuple(
        Nozzle(
            id=_required(node, "id"),
            min_part_mm=_float(node, "min_part"),
            max_part_mm=_float(node, "max_part"),
            compatible_package_ids=tuple(filter(None, node.get("compatible_packages", "").split(","))),
        )
        for node in root.findall(resource_path + f"/{{{_CHUDWARE_NS}}}nozzle")
    )
    calibrations = tuple(
        CalibrationRecord(
            id=_required(node, "id"),
            subject_id=_required(node, "subject_id"),
            kind=_required(node, "kind"),
            method=_required(node, "method"),
            performed_at=_required(node, "performed_at"),
            source=node.get("source", "simulated"),
            uncertainty_mm=float(node.attrib["uncertainty"]) if node.get("uncertainty") else None,
            evidence_refs=tuple(filter(None, node.get("evidence_refs", "").split(","))),
        )
        for node in root.findall(resource_path + f"/{{{_CHUDWARE_NS}}}calibration")
    )
    return PnpJob(
        id=root.get("id", job_path.stem),
        board=board,
        packages=packages,
        feeders=feeders,
        nozzles=nozzles,
        calibrations=calibrations,
        panel=panel,
    )
