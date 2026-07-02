"""Firmware toolchain wrappers — build a :class:`~engine.firmware.project.FirmwareProject`.

Every backend drives an **unmodified external CLI as a subprocess** (never linked) — GPL-clean by
aggregation, the ngspice posture from ``contracts/licenses.md``. :meth:`Toolchain.build` NEVER
raises: a missing CLI, a non-zero exit, or a timeout all come back as a :class:`BuildResult` with
``success=False`` (``returncode == -1`` marks "tool not found / timed out"), so the RPC layer can map
it to the ``toolchain_not_found`` error envelope (``firmware_rpc.md`` §1/§5) rather than crashing.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.firmware.project import FirmwareProject

_DEFAULT_TIMEOUT_S = 300.0
_STDERR_CAP = 64_000  # keep captured stderr bounded (contract §3)


@dataclass
class BuildResult:
    """See ``contracts/firmware_rpc.md`` §3."""
    success: bool
    elf_path: Optional[str] = None
    map_path: Optional[str] = None
    stderr: str = ""
    warnings: list[str] = field(default_factory=list)
    returncode: int = 0
    cmd: list[str] = field(default_factory=list)
    ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "elf_path": self.elf_path,
            "map_path": self.map_path,
            "stderr": self.stderr,
            "warnings": self.warnings,
            "returncode": self.returncode,
            "cmd": self.cmd,
            "ms": round(self.ms, 2),
        }


# --------------------------------------------------------------------------- helpers


def _cap(text: str) -> str:
    return text if len(text) <= _STDERR_CAP else (text[:_STDERR_CAP] + "\n…[truncated]")


def _parse_warnings(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ": warning:" in ln or ln.strip().startswith("warning:")]


def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
    """Run ``cmd`` in ``cwd``. Returns (returncode, stdout, stderr). A missing executable or a
    timeout maps to returncode ``-1`` (never raises)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return -1, "", f"toolchain_not_found: executable '{cmd[0]}' not on PATH"
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout: '{cmd[0]}' exceeded {timeout:.0f}s"


def _not_found(tool: str, cmd: list[str], t0: float) -> BuildResult:
    return BuildResult(
        success=False, returncode=-1, cmd=cmd, ms=(time.perf_counter() - t0) * 1000.0,
        stderr=f"toolchain_not_found: '{tool}' is not installed / not on PATH",
    )


def _first_glob(base: Path, *patterns: str) -> Optional[str]:
    for pat in patterns:
        for hit in sorted(base.rglob(pat)):
            if hit.is_file():
                return str(hit)
    return None


# --------------------------------------------------------------------------- toolchains


class Toolchain:
    """Abstract backend. Subclasses implement :meth:`build`; all are subprocess-only."""

    name = "abstract"
    required_tools: tuple[str, ...] = ()

    def available(self) -> bool:
        return all(shutil.which(t) is not None for t in self.required_tools)

    def build(self, project: FirmwareProject, *, timeout: float = _DEFAULT_TIMEOUT_S) -> BuildResult:
        raise NotImplementedError


class CMakeToolchain(Toolchain):
    """Default baseline: CMake configure (exporting compile_commands.json) + build with the
    ``arm-none-eabi`` cross-compiler selected by the template's toolchain file."""

    name = "cmake"
    required_tools = ("cmake",)

    def build(self, project: FirmwareProject, *, timeout: float = _DEFAULT_TIMEOUT_S) -> BuildResult:
        t0 = time.perf_counter()
        root = project.root_dir
        build_dir = project.build_path
        if shutil.which("cmake") is None:
            return _not_found("cmake", ["cmake"], t0)

        generator = "Ninja" if shutil.which("ninja") else "Unix Makefiles"
        tc_file = root / "cmake" / "arm-none-eabi-toolchain.cmake"
        configure = [
            "cmake", "-S", str(root), "-B", str(build_dir),
            "-G", generator,
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        if tc_file.is_file():
            configure.append(f"-DCMAKE_TOOLCHAIN_FILE={tc_file}")

        rc, out, err = _run(configure, root, timeout)
        combined = out + err
        if rc != 0:
            return BuildResult(
                success=False, returncode=rc, cmd=configure, stderr=_cap(combined),
                warnings=_parse_warnings(combined), ms=(time.perf_counter() - t0) * 1000.0,
            )

        build = ["cmake", "--build", str(build_dir)]
        rc, out, err = _run(build, root, timeout)
        combined += out + err
        elf = _first_glob(build_dir, "*.elf", "*.bin", "*.hex")
        map_path = _first_glob(build_dir, "*.map")
        return BuildResult(
            success=(rc == 0 and elf is not None),
            elf_path=elf, map_path=map_path,
            returncode=rc, cmd=build, stderr=_cap(combined),
            warnings=_parse_warnings(combined), ms=(time.perf_counter() - t0) * 1000.0,
        )


class ESPIDFToolchain(Toolchain):
    """Espressif ESP-IDF backend — drives ``idf.py build`` (Apache-2.0, subprocess)."""

    name = "espidf"
    required_tools = ("idf.py",)

    def build(self, project: FirmwareProject, *, timeout: float = _DEFAULT_TIMEOUT_S) -> BuildResult:
        t0 = time.perf_counter()
        root = project.root_dir
        if shutil.which("idf.py") is None:
            return _not_found("idf.py", ["idf.py", "build"], t0)
        cmd = ["idf.py", "-C", str(root), "build"]
        rc, out, err = _run(cmd, root, timeout)
        combined = out + err
        elf = _first_glob(project.build_path, "*.elf", "*.bin")
        map_path = _first_glob(project.build_path, "*.map")
        return BuildResult(
            success=(rc == 0 and elf is not None),
            elf_path=elf, map_path=map_path,
            returncode=rc, cmd=cmd, stderr=_cap(combined),
            warnings=_parse_warnings(combined), ms=(time.perf_counter() - t0) * 1000.0,
        )


class ArduinoToolchain(Toolchain):
    """Arduino CLI backend — ``arduino-cli compile`` (GPL-3, subprocess-only)."""

    name = "arduino"
    required_tools = ("arduino-cli",)

    def build(self, project: FirmwareProject, *, timeout: float = _DEFAULT_TIMEOUT_S) -> BuildResult:
        t0 = time.perf_counter()
        root = project.root_dir
        if shutil.which("arduino-cli") is None:
            return _not_found("arduino-cli", ["arduino-cli", "compile"], t0)
        out_dir = project.build_path
        cmd = [
            "arduino-cli", "compile",
            "--fqbn", project.mcu_family,
            "--output-dir", str(out_dir),
            str(root),
        ]
        rc, out, err = _run(cmd, root, timeout)
        combined = out + err
        elf = _first_glob(out_dir, "*.elf", "*.bin", "*.hex")
        map_path = _first_glob(out_dir, "*.map")
        return BuildResult(
            success=(rc == 0 and elf is not None),
            elf_path=elf, map_path=map_path,
            returncode=rc, cmd=cmd, stderr=_cap(combined),
            warnings=_parse_warnings(combined), ms=(time.perf_counter() - t0) * 1000.0,
        )


_TOOLCHAINS: dict[str, type[Toolchain]] = {
    "cmake": CMakeToolchain,
    "espidf": ESPIDFToolchain,
    "arduino": ArduinoToolchain,
}


def get_toolchain(name: str) -> Toolchain:
    cls = _TOOLCHAINS.get(name)
    if cls is None:
        raise ValueError(f"unknown toolchain {name!r}; expected one of {tuple(_TOOLCHAINS)}")
    return cls()


def build(project: FirmwareProject, *, timeout: float = _DEFAULT_TIMEOUT_S) -> BuildResult:
    """Build ``project`` with the backend named by ``project.toolchain``."""
    return get_toolchain(project.toolchain).build(project, timeout=timeout)
