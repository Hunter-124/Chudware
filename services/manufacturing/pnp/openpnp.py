"""OpenPnP-shaped board/job XML file interchange.

The interchange is intentionally limited to files.  It does not import OpenPnP
code, start a JVM, discover devices, or expose a driver.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import tempfile
import xml.etree.ElementTree as ET

from .model import (
    Board, CalibrationRecord, Feeder, Fiducial, Nozzle, Package, Panel,
    PanelInstance, Placement, PnpJob,
)

_VERSION = "1.1"
_UNITS = "Millimeters"
_CHUDWARE_NS = "urn:chudware:pnp:1"
MAX_XML_BYTES = 4 * 1024 * 1024
MAX_ELEMENTS = 100_000
ET.register_namespace("chudware", _CHUDWARE_NS)

class OpenPnpRefusal(ValueError):
    """Named refusal for unsafe or resource-exhausting OpenPnP input."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _safe_path(
    source: str | Path,
    *,
    label: str,
    root: Path | None = None,
    require_relative: bool = False,
) -> Path:
    raw = Path(source).expanduser()
    if ".." in raw.parts:
        raise OpenPnpRefusal(f"{label}_traversal", f"{label} may not contain parent traversal")
    if require_relative and raw.is_absolute():
        raise OpenPnpRefusal(f"{label}_absolute", f"{label} must be relative")
    candidate = raw if raw.is_absolute() else (root or Path.cwd()) / raw
    candidate = Path(os.path.abspath(candidate))
    if root is not None and not candidate.is_relative_to(root):
        raise OpenPnpRefusal(f"{label}_escape", f"{label} is outside the allowed root")
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise OpenPnpRefusal(f"{label}_missing", f"{label} is missing") from None
        except OSError:
            raise OpenPnpRefusal(f"{label}_unreadable", f"{label} could not be inspected") from None
        if stat.S_ISLNK(mode):
            raise OpenPnpRefusal(f"{label}_symlink", f"{label} may not use symlinks")
    return candidate


def _read_xml(path: Path, *, label: str) -> ET.Element:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OpenPnpRefusal(f"{label}_symlink", f"{label} may not use symlinks") from None
        raise OpenPnpRefusal(f"{label}_unreadable", f"{label} could not be read") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OpenPnpRefusal(f"{label}_not_regular", f"{label} must be a regular file")
        if metadata.st_size > MAX_XML_BYTES:
            raise OpenPnpRefusal(
                f"{label}_too_large",
                f"{label} exceeds the {MAX_XML_BYTES} byte limit",
            )
        payload = bytearray()
        while chunk := os.read(fd, min(1024 * 1024, MAX_XML_BYTES + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > MAX_XML_BYTES:
                raise OpenPnpRefusal(
                    f"{label}_too_large",
                    f"{label} exceeds the {MAX_XML_BYTES} byte limit",
                )
    finally:
        os.close(fd)
    try:
        text = bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        raise OpenPnpRefusal(f"{label}_encoding", f"{label} is not UTF-8") from None
    folded = text.casefold()
    if "<!doctype" in folded or "<!entity" in folded:
        raise OpenPnpRefusal(
            f"{label}_declaration",
            f"{label} may not contain DTD or entity declarations",
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise OpenPnpRefusal(f"{label}_malformed", f"{label} is not well-formed XML") from None
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        raise OpenPnpRefusal(
            f"{label}_element_limit",
            f"{label} exceeds the {MAX_ELEMENTS} element limit",
        )
    return root


def _f(value: float) -> str:
    return format(value, ".15g")


def _location(parent: ET.Element, x: float, y: float, rotation: float = 0.0) -> None:
    ET.SubElement(parent, "location", units=_UNITS, x=_f(x), y=_f(y), z="0", rotation=_f(rotation))


def _write_xml(root: ET.Element, path: Path) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(path.parent, label="openpnp_output_directory")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise OpenPnpRefusal(
                    "openpnp_output_symlink",
                    "OpenPnP output may not replace or follow a symlink",
                )
            if not stat.S_ISREG(mode):
                raise OpenPnpRefusal(
                    "openpnp_output_not_regular",
                    "OpenPnP output must be a regular file",
                )
        ET.indent(root, space="  ")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            ET.ElementTree(root).write(
                stream,
                encoding="utf-8",
                xml_declaration=True,
                short_empty_elements=True,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OpenPnpRefusal:
        raise
    except OSError:
        raise OpenPnpRefusal(
            "openpnp_output_unavailable",
            "OpenPnP output could not be written",
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_openpnp_job(job: PnpJob, path: str | Path) -> Path:
    """Emit an OpenPnP ``.job.xml`` plus its referenced ``.board.xml``.

    Chudware-only metadata is stored in namespaced attributes/elements. OpenPnP
    readers ignore those while this importer uses them for lossless round trips.
    """
    instances = (
        job.panel.instances
        if job.panel is not None
        else (PanelInstance(id=f"{job.board.id}-1", x_mm=0.0, y_mm=0.0),)
    )
    element_count = (
        10
        + len(job.packages)
        + 3 * len(job.board.placements)
        + 2 * len(job.board.fiducials)
        + 2 * len(instances)
        + len(job.feeders)
        + len(job.nozzles)
        + len(job.calibrations)
        + (2 * len(job.panel.fiducials) + 2 if job.panel is not None else 0)
    )
    if element_count > MAX_ELEMENTS:
        raise OpenPnpRefusal(
            "openpnp_export_element_limit",
            f"OpenPnP export exceeds the {MAX_ELEMENTS} element limit",
        )
    job_path = Path(path)
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


def import_openpnp_job(
    path: str | Path,
    *,
    root_dir: str | Path | None = None,
) -> PnpJob:
    """Parse bounded OpenPnP XML without following paths outside its input root."""
    lexical = Path(path).expanduser()
    root_path = None
    if root_dir is not None:
        root_path = _safe_path(root_dir, label="openpnp_root")
    job_path = _safe_path(lexical, label="openpnp_job", root=root_path)
    root = _read_xml(job_path, label="openpnp_job")
    if root.tag != "openpnp-job":
        raise ValueError("expected OpenPnP <openpnp-job> root")
    board_locations = root.findall("./board-locations/board-location")
    if not board_locations:
        raise ValueError("OpenPnP job contains no board locations")
    board_file = _required(board_locations[0], "board_file")
    if any(_required(node, "board_file") != board_file for node in board_locations):
        raise ValueError("this offline interchange supports one board revision per job")
    board_path = _safe_path(
        board_file,
        label="openpnp_board",
        root=job_path.parent,
        require_relative=True,
    )
    board_root = _read_xml(board_path, label="openpnp_board")
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
