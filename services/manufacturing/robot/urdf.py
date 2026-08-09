"""Hardened stdlib XML import for serial-chain URDF and SRDF files."""
from __future__ import annotations

import errno
import math
import os
from pathlib import Path
import stat
import xml.etree.ElementTree as ET

from .model import JointLimit, RobotChain, RobotJoint, SrdfModel

MAX_XML_BYTES = 4 * 1024 * 1024
MAX_ELEMENTS = 100_000


class RobotXmlRefusal(ValueError):
    """Named refusal for unsafe or resource-exhausting URDF/SRDF input."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _safe_xml_path(
    source: str | Path,
    *,
    root_dir: str | Path | None,
) -> Path:
    raw = Path(source).expanduser()
    if ".." in raw.parts:
        raise RobotXmlRefusal("robot_xml_traversal", "robot XML path may not contain parent traversal")
    root = None
    if root_dir is not None:
        root_raw = Path(root_dir).expanduser()
        root = root_raw if root_raw.is_absolute() else Path.cwd() / root_raw
        root = Path(os.path.abspath(root))
    candidate = raw if raw.is_absolute() else (root or Path.cwd()) / raw
    candidate = Path(os.path.abspath(candidate))
    if root is not None and not candidate.is_relative_to(root):
        raise RobotXmlRefusal("robot_xml_escape", "robot XML path is outside the allowed root")
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise RobotXmlRefusal("robot_xml_missing", "robot XML input is missing") from None
        except OSError:
            raise RobotXmlRefusal(
                "robot_xml_unreadable",
                "robot XML input could not be inspected",
            ) from None
        if stat.S_ISLNK(mode):
            raise RobotXmlRefusal("robot_xml_symlink", "robot XML input may not use symlinks")
    return candidate


def _bounded_xml_text(source: str | Path, *, root_dir: str | Path | None) -> str:
    if isinstance(source, Path) or (isinstance(source, str) and "<" not in source):
        path = _safe_xml_path(source, root_dir=root_dir)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RobotXmlRefusal("robot_xml_symlink", "robot XML input may not use symlinks") from None
            raise RobotXmlRefusal("robot_xml_unreadable", "robot XML input could not be read") from None
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise RobotXmlRefusal("robot_xml_not_regular", "robot XML input must be a regular file")
            if metadata.st_size > MAX_XML_BYTES:
                raise RobotXmlRefusal(
                    "robot_xml_too_large",
                    f"robot XML input exceeds the {MAX_XML_BYTES} byte limit",
                )
            payload = bytearray()
            while chunk := os.read(fd, min(1024 * 1024, MAX_XML_BYTES + 1 - len(payload))):
                payload.extend(chunk)
                if len(payload) > MAX_XML_BYTES:
                    raise RobotXmlRefusal(
                        "robot_xml_too_large",
                        f"robot XML input exceeds the {MAX_XML_BYTES} byte limit",
                    )
        finally:
            os.close(fd)
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            raise RobotXmlRefusal("robot_xml_encoding", "robot XML input is not UTF-8") from None
    text = str(source)
    if len(text.encode("utf-8")) > MAX_XML_BYTES:
        raise RobotXmlRefusal(
            "robot_xml_too_large",
            f"robot XML input exceeds the {MAX_XML_BYTES} byte limit",
        )
    return text


def _xml_root(
    source: str | Path,
    expected: str,
    *,
    root_dir: str | Path | None = None,
) -> ET.Element:
    text = _bounded_xml_text(source, root_dir=root_dir)
    folded = text.casefold()
    if "<!doctype" in folded or "<!entity" in folded:
        raise RobotXmlRefusal(
            "robot_xml_declaration",
            "robot XML may not contain DTD or entity declarations",
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise RobotXmlRefusal("robot_xml_malformed", "robot XML is not well-formed") from None
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        raise RobotXmlRefusal(
            "robot_xml_element_limit",
            f"robot XML exceeds the {MAX_ELEMENTS} element limit",
        )
    if root.tag != expected:
        raise RobotXmlRefusal(
            "robot_xml_wrong_root",
            f"robot XML must have a <{expected}> root",
        )
    return root


def _vec(text: str | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if text is None:
        return default
    values = tuple(float(part) for part in text.split())
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError(f"expected three finite values, got {text!r}")
    return values  # type: ignore[return-value]


def _normal(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1e-15:
        raise ValueError("joint axis must be non-zero")
    return tuple(value / length for value in vector)  # type: ignore[return-value]


def import_urdf(
    source: str | Path,
    *,
    root_link: str | None = None,
    tip_link: str | None = None,
    root_dir: str | Path | None = None,
) -> RobotChain:
    """Import one bounded unbranched path from URDF, normalising metres to millimetres."""
    root = _xml_root(source, "robot", root_dir=root_dir)
    links = tuple(node.attrib["name"] for node in root.findall("link"))
    if not links:
        raise ValueError("URDF has no links")
    joints: list[RobotJoint] = []
    for node in root.findall("joint"):
        parent_node, child_node = node.find("parent"), node.find("child")
        if parent_node is None or child_node is None:
            raise ValueError(f"joint {node.get('name', '<unnamed>')} lacks parent or child")
        origin = node.find("origin")
        axis_node = node.find("axis")
        limit_node = node.find("limit")
        joint_type = node.get("type", "fixed")
        limit = None
        if joint_type in {"revolute", "prismatic"}:
            if limit_node is None:
                raise ValueError(f"joint {node.get('name')} requires limits")
            limit = JointLimit(
                lower=float(limit_node.get("lower", "0")),
                upper=float(limit_node.get("upper", "0")),
                velocity=float(limit_node.attrib["velocity"]) if "velocity" in limit_node.attrib else None,
            )
        joints.append(
            RobotJoint(
                name=node.attrib["name"],
                joint_type=joint_type,
                parent=parent_node.attrib["link"],
                child=child_node.attrib["link"],
                origin_xyz_mm=tuple(value * 1000.0 for value in _vec(origin.get("xyz") if origin is not None else None, (0.0, 0.0, 0.0))),  # type: ignore[arg-type]
                origin_rpy_rad=_vec(origin.get("rpy") if origin is not None else None, (0.0, 0.0, 0.0)),
                axis=_normal(_vec(axis_node.get("xyz") if axis_node is not None else None, (1.0, 0.0, 0.0))),
                limit=limit,
            )
        )
    children = {joint.child for joint in joints}
    selected_root = root_link or next((link for link in links if link not in children), None)
    if selected_root is None or selected_root not in links:
        raise ValueError("URDF serial chain has no unique root")
    by_parent: dict[str, list[RobotJoint]] = {}
    for joint in joints:
        by_parent.setdefault(joint.parent, []).append(joint)
    reaching_tip = _nodes_reaching(tip_link, joints) if tip_link is not None else None
    path: list[RobotJoint] = []
    current = selected_root
    seen = {current}
    while current != tip_link:
        options = by_parent.get(current, [])
        if not options:
            break
        if len(options) != 1:
            if tip_link is None:
                raise ValueError(f"URDF branches at {current}; specify a serial-chain tip")
            candidates = [joint for joint in options if joint.child in reaching_tip]
            if len(candidates) != 1:
                raise ValueError(f"tip {tip_link} does not select one branch at {current}")
            options = candidates
        joint = options[0]
        if joint.child in seen:
            raise ValueError("URDF joint graph contains a cycle")
        path.append(joint)
        current = joint.child
        seen.add(current)
    if tip_link is not None and current != tip_link:
        raise ValueError(f"tip link {tip_link!r} is unreachable from {selected_root!r}")
    if not path:
        raise ValueError("URDF selected chain contains no joints")
    path_links = (selected_root,) + tuple(joint.child for joint in path)
    return RobotChain(root.get("name", "robot"), selected_root, current, path_links, tuple(path))


def _nodes_reaching(target: str, joints: list[RobotJoint]) -> set[str]:
    by_child: dict[str, list[str]] = {}
    for joint in joints:
        by_child.setdefault(joint.child, []).append(joint.parent)
    reachable = {target}
    pending = [target]
    while pending:
        child = pending.pop()
        for parent in by_child.get(child, ()):
            if parent not in reachable:
                reachable.add(parent)
                pending.append(parent)
    return reachable


def import_srdf(
    source: str | Path,
    *,
    root_dir: str | Path | None = None,
) -> SrdfModel:
    root = _xml_root(source, "robot", root_dir=root_dir)
    groups: dict[str, tuple[str, str]] = {}
    for group in root.findall("group"):
        chain = group.find("chain")
        if chain is not None:
            groups[group.attrib["name"]] = (chain.attrib["base_link"], chain.attrib["tip_link"])
    states: dict[str, dict[str, float]] = {}
    for state in root.findall("group_state"):
        states[state.attrib["name"]] = {node.attrib["name"]: float(node.attrib["value"]) for node in state.findall("joint")}
    end_effectors = {
        node.attrib["name"]: (node.attrib["parent_link"], node.attrib["group"])
        for node in root.findall("end_effector")
    }
    disabled = tuple(
        (node.attrib["link1"], node.attrib["link2"], node.get("reason", ""))
        for node in root.findall("disable_collisions")
    )
    return SrdfModel(root.get("name", "robot"), groups, states, end_effectors, disabled)
