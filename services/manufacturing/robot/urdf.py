"""Hardened stdlib XML import for serial-chain URDF and SRDF files."""
from __future__ import annotations

from pathlib import Path
import math
import xml.etree.ElementTree as ET

from .model import JointLimit, RobotChain, RobotJoint, SrdfModel


def _xml_root(source: str | Path, expected: str) -> ET.Element:
    if isinstance(source, Path) or (isinstance(source, str) and "<" not in source):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = str(source)
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("DTD and entity declarations are forbidden in robot XML")
    root = ET.fromstring(text)
    if root.tag != expected:
        raise ValueError(f"expected <{expected}> root, got <{root.tag}>")
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


def import_urdf(source: str | Path, *, root_link: str | None = None, tip_link: str | None = None) -> RobotChain:
    """Import one unbranched path from URDF, normalising metres to millimetres."""
    root = _xml_root(source, "robot")
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
            candidates = [joint for joint in options if _can_reach(joint.child, tip_link, by_parent, set())]
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


def _can_reach(current: str, target: str, by_parent: dict[str, list[RobotJoint]], seen: set[str]) -> bool:
    if current == target:
        return True
    if current in seen:
        return False
    seen.add(current)
    return any(_can_reach(joint.child, target, by_parent, seen.copy()) for joint in by_parent.get(current, ()))


def import_srdf(source: str | Path) -> SrdfModel:
    root = _xml_root(source, "robot")
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
