"""Firmware project record — the :class:`FirmwareProject` dataclass + create/save/load.

A project is a directory on disk (``<firmware_root>/<project_id>/``) holding the toolchain sources
(seeded from a template) and a ``project.json`` sidecar with the record. Persistence uses the atomic
tmp+``os.replace`` idiom from :mod:`engine.ecad.autosave`. See ``contracts/firmware_rpc.md`` §2/§6.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

SCHEMA = "aicad.firmware.project/1"

# Source extensions the toolchains compile — used to seed src_paths and synthesize
# compile_commands.json from a template that has no committed build directory.
_SRC_EXTS = (".c", ".cpp", ".cc", ".cxx", ".S", ".s")

# Templates shipped with the package (one dir per toolchain baseline).
_TEMPLATES = Path(__file__).resolve().parent / "templates"
_TEMPLATE_FOR = {
    "cmake": "cmake_arm",
    "espidf": "cmake_arm",   # ESP-IDF projects also start from a CMake tree; idf.py drives it
    "arduino": "cmake_arm",  # placeholder until an Arduino sketch template lands (later slice)
}

_VALID_TOOLCHAINS = ("cmake", "espidf", "arduino")


def firmware_root() -> Path:
    """Base directory holding every firmware project. ``AI_CAD_FIRMWARE_DIR`` overrides it wholesale
    (tests), else ``%APPDATA%/AI-CAD/firmware`` (falling back to the temp dir when APPDATA is unset).
    Mirrors :func:`engine.ecad.autosave.autosave_dir`."""
    override = os.environ.get("AI_CAD_FIRMWARE_DIR")
    if override:
        d = Path(override)
    else:
        base = os.environ.get("APPDATA") or tempfile.gettempdir()
        d = Path(base) / "AI-CAD" / "firmware"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class FirmwareProject:
    project_id: str
    name: str
    mcu_family: str
    toolchain: str = "cmake"
    board_doc_ref: Optional[str] = None
    src_paths: list[str] = field(default_factory=list)   # relative to root
    build_dir: str = "build"                              # relative to root
    created_at: float = 0.0
    # Absolute project root. Not serialized (recomputed on load from firmware_root/project_id) so a
    # project directory can be relocated without stale paths — see to_dict/from_dict.
    root: Optional[Path] = None

    # -- derived paths -----------------------------------------------------

    @property
    def root_dir(self) -> Path:
        return self.root if self.root is not None else (firmware_root() / self.project_id)

    @property
    def build_path(self) -> Path:
        return self.root_dir / self.build_dir

    @property
    def sidecar(self) -> Path:
        return self.root_dir / "project.json"

    def abs_sources(self) -> list[Path]:
        return [self.root_dir / p for p in self.src_paths]

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("root", None)          # absolute path is environment-specific; recomputed on load
        d["schema"] = SCHEMA
        return d

    @classmethod
    def from_dict(cls, d: dict, *, root: Optional[Path] = None) -> "FirmwareProject":
        return cls(
            project_id=d["project_id"],
            name=d.get("name", d["project_id"]),
            mcu_family=d.get("mcu_family", ""),
            toolchain=d.get("toolchain", "cmake"),
            board_doc_ref=d.get("board_doc_ref"),
            src_paths=list(d.get("src_paths", [])),
            build_dir=d.get("build_dir", "build"),
            created_at=float(d.get("created_at", 0.0)),
            root=root,
        )


# --------------------------------------------------------------------------- persistence


def save(project: FirmwareProject) -> Path:
    """Atomically write ``project.json`` into the project root (tmp + os.replace)."""
    root = project.root_dir
    root.mkdir(parents=True, exist_ok=True)
    final = root / "project.json"
    tmp = root / ".project.json.tmp"
    tmp.write_text(json.dumps(project.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, final)  # atomic on POSIX + Windows
    return final


def load(project_id: str, *, root: Optional[Path] = None) -> FirmwareProject:
    """Load a project by id from ``<firmware_root>/<project_id>/project.json``."""
    base = root if root is not None else (firmware_root() / project_id)
    sidecar = base / "project.json"
    if not sidecar.is_file():
        raise FileNotFoundError(f"no firmware project '{project_id}' at {sidecar}")
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    return FirmwareProject.from_dict(data, root=base)


# --------------------------------------------------------------------------- template install


def _template_dir(toolchain: str) -> Path:
    name = _TEMPLATE_FOR.get(toolchain, "cmake_arm")
    d = _TEMPLATES / name
    if not d.is_dir():
        raise FileNotFoundError(f"firmware template '{name}' not found at {d}")
    return d


def _install_template(root: Path, toolchain: str) -> list[str]:
    """Copy the toolchain template into ``root`` and return the source files (relative POSIX paths)
    discovered in it, for the project's ``src_paths``."""
    tmpl = _template_dir(toolchain)
    shutil.copytree(tmpl, root, dirs_exist_ok=True)
    srcs: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in _SRC_EXTS:
            srcs.append(p.relative_to(root).as_posix())
    return srcs


def _synth_compile_commands(project: FirmwareProject) -> Path:
    """Write a synthetic ``compile_commands.json`` enumerating the project's sources so IDE tooling
    (clangd) works before any real CMake configure. A subsequent real configure (in the CMake
    toolchain's build()) regenerates the accurate database and this file is overwritten. See
    ``contracts/firmware_rpc.md`` §6."""
    root = project.root_dir
    entries = [
        {
            "directory": str(root),
            "command": f"arm-none-eabi-gcc -c {src.as_posix()}",
            "file": str(src),
        }
        for src in project.abs_sources()
    ]
    out = root / "compile_commands.json"
    out.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- create


def new_project(board_doc_ref: Optional[str], mcu: Optional[str] = None, toolchain: str = "cmake",
                *, name: Optional[str] = None, root: Optional[Path] = None,
                board_doc: Optional[dict] = None) -> FirmwareProject:
    """Create a new firmware project directory seeded from the toolchain template.

    Creates ``<firmware_root>/<project_id>/`` (or ``root`` if given), copies the CMake template,
    records ``src_paths`` from the template, writes a synthetic ``compile_commands.json`` (so the
    contract guarantee in §6 holds without a cross-toolchain installed), and persists ``project.json``.

    ``mcu`` (the firmware ``mcu_family``) is **optional** (T-C2): when omitted, the family is
    auto-selected from the linked ECAD BOM — the first MCU component of ``board_doc`` (or the doc
    that ``board_doc_ref`` resolves to). If no family can be given or derived, raises ``ValueError``
    rather than guessing an MCU. Passing ``mcu`` explicitly keeps the original behaviour unchanged.
    """
    if toolchain not in _VALID_TOOLCHAINS:
        raise ValueError(f"toolchain must be one of {_VALID_TOOLCHAINS}, got {toolchain!r}")

    if not mcu:
        from engine.firmware import mcu_pinouts
        doc = board_doc if board_doc is not None else mcu_pinouts.resolve_board_doc(board_doc_ref)
        if doc is not None:
            mcu = mcu_pinouts.select_mcu_family(doc)
        if not mcu:
            raise ValueError(
                "new_project: no `mcu` given and none derivable from the ECAD BOM "
                "(need an STM32*/ESP32*/ATmega* MCU component in board_doc_ref, or pass mcu "
                "explicitly)")

    project_id = uuid.uuid4().hex[:12]
    proj_root = Path(root) if root is not None else (firmware_root() / project_id)
    proj_root.mkdir(parents=True, exist_ok=True)

    src_paths = _install_template(proj_root, toolchain)

    project = FirmwareProject(
        project_id=project_id,
        name=name or f"firmware-{project_id[:6]}",
        mcu_family=mcu,
        toolchain=toolchain,
        board_doc_ref=board_doc_ref,
        src_paths=src_paths,
        build_dir="build",
        created_at=time.time(),
        root=proj_root,
    )
    _synth_compile_commands(project)
    save(project)
    return project
