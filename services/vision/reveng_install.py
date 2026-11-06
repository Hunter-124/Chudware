"""In-app installer for the Reverse Engineering subprocess tools (Brush, OpenMVS).

Both are SHA-256-pinned upstream Linux x86_64 releases fetched into the managed tools
directory (``reveng_provision.brush_install_dir`` / ``openmvs_install_dir``) and only ever run
as subprocesses. This module ships with the app so a customer never needs the repo or
``scripts/provision_reveng.py``; that script is a thin CLI over the same functions.

``start_install`` runs the downloads on one background thread and publishes progress through
``install_state`` (surfaced as ``reveng_status().install``), so the GUI and agents can poll it.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from services.vision.reveng_provision import (
    BRUSH_SHA256, BRUSH_SOURCE, BRUSH_URL, BRUSH_VERSION, OPENMVS_SHA256, OPENMVS_TOOLS,
    OPENMVS_URL, OPENMVS_VERSION, brush_install_dir, openmvs_install_dir,
)

BRUSH_REPOSITORY = "https://github.com/ArthurBrussee/brush.git"
TOOLS = ("brush", "openmvs")

Progress = Callable[[str, int, int], None]   # (message, bytes_done, bytes_total)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def supported() -> bool:
    return platform.system() == "Linux" and platform.machine().lower() in ("x86_64", "amd64")


def _download(url: str, dest: Path, sha256: str, limit: int, label: str,
              progress: Optional[Progress]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Chudware-Reveng-Installer/1"})
    checksum = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, dest.open("xb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"Release exceeds the {limit // 1024 ** 2} MiB download limit")
            checksum.update(chunk)
            output.write(chunk)
            if progress:
                progress(f"Downloading {label}", size, total)
    if checksum.hexdigest() != sha256:
        raise ValueError(f"{label} release SHA256 mismatch: expected {sha256}, "
                         f"received {checksum.hexdigest()}")


def extract_release(archive: Path, destination: Path) -> Path:
    """Extract only regular files/directories; never follow archive links."""
    with tarfile.open(archive, "r:xz") as bundle:
        members = bundle.getmembers()
        seen = set()
        total = 0
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Archive links/devices are forbidden: {member.name}")
            if name in seen:
                raise ValueError(f"Duplicate archive path: {member.name}")
            seen.add(name)
            total += member.size
            if total > 4 * 1024 ** 3:
                raise ValueError("Release exceeds the 4 GiB extraction limit")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"Missing archive member: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    binaries = list(destination.rglob("brush_app"))
    if len(binaries) != 1 or not binaries[0].is_file():
        raise ValueError("Release must contain exactly one regular brush_app executable")
    executable = binaries[0]
    with executable.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError("Release brush_app is not an ELF executable")
    executable.chmod(0o755)
    return executable


def install_brush(progress: Optional[Progress] = None) -> Path:
    if not supported():
        raise ValueError("The pinned Brush binary is Linux x86_64 only; set BRUSH_BIN to your own build.")
    target = brush_install_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = target / "install.json"
    if target.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
            executable = target / "brush_app"
            if (receipt["archive_sha256"] == BRUSH_SHA256
                    and receipt["source_commit"] == BRUSH_SOURCE
                    and receipt["executable_sha256"] == digest(executable)
                    and os.access(executable, os.X_OK)):
                return executable
        except (OSError, ValueError, KeyError):
            pass
        raise ValueError(f"Existing install is not a verified Brush {BRUSH_VERSION} release: {target}. "
                         "Move it aside before reinstalling.")
    with tempfile.TemporaryDirectory(prefix=".brush-install-", dir=target.parent) as work:
        work = Path(work)
        archive = work / "release.tar.xz"
        _download(BRUSH_URL, archive, BRUSH_SHA256, 2 * 1024 ** 3, "Brush", progress)
        if progress:
            progress("Unpacking Brush", 0, 0)
        extracted = work / "extracted"
        extracted.mkdir()
        executable = extract_release(archive, extracted)
        payload = executable.parent   # upstream may wrap the app and resources in one directory
        receipt = {"version": BRUSH_VERSION, "license": "Apache-2.0",
                   "source_repository": BRUSH_REPOSITORY, "source_commit": BRUSH_SOURCE,
                   "archive_url": BRUSH_URL, "archive_sha256": BRUSH_SHA256,
                   "executable_sha256": digest(executable)}
        (payload / "install.json").write_text(json.dumps(receipt, indent=2) + "\n")
        payload.rename(target)
    return target / "brush_app"


def install_openmvs(progress: Optional[Progress] = None) -> Path:
    """Fetch the SHA-256-pinned upstream OpenMVS (AGPL-3.0) release; subprocess use only."""
    if not supported():
        raise ValueError("The pinned OpenMVS binaries are Linux x86_64 only; install OpenMVS "
                         "yourself and set OPENMVS_BIN.")
    target = openmvs_install_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = target / "install.json"
    if target.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
            if (receipt["archive_sha256"] == OPENMVS_SHA256
                    and all(receipt["tools"][n] == digest(target / n) and os.access(target / n, os.X_OK)
                            for n in OPENMVS_TOOLS)):
                return target
        except (OSError, ValueError, KeyError):
            pass
        raise ValueError(f"Existing install is not a verified OpenMVS {OPENMVS_VERSION} release: "
                         f"{target}. Move it aside before reinstalling.")
    with tempfile.TemporaryDirectory(prefix=".openmvs-install-", dir=target.parent) as work:
        work = Path(work)
        archive = work / "release.zip"
        _download(OPENMVS_URL, archive, OPENMVS_SHA256, 1024 ** 3, "OpenMVS", progress)
        if progress:
            progress("Unpacking OpenMVS", 0, 0)
        payload = work / "payload"
        payload.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            members = {info.filename: info for info in bundle.infolist()}
            for name in OPENMVS_TOOLS:
                info = members.get(name)
                if info is None or info.is_dir():
                    raise ValueError(f"Release is missing the {name} executable")
                out = payload / name
                with bundle.open(info) as source, out.open("xb") as output:
                    shutil.copyfileobj(source, output)
                with out.open("rb") as stream:
                    if stream.read(4) != b"\x7fELF":
                        raise ValueError(f"Release {name} is not an ELF executable")
                out.chmod(0o755)
        receipt = {"version": OPENMVS_VERSION, "license": "AGPL-3.0",
                   "source_repository": "https://github.com/cdcseacave/openMVS",
                   "archive_url": OPENMVS_URL, "archive_sha256": OPENMVS_SHA256,
                   "tools": {n: digest(payload / n) for n in OPENMVS_TOOLS}}
        (payload / "install.json").write_text(json.dumps(receipt, indent=2) + "\n")
        payload.rename(target)
    return target


_INSTALLERS = {"brush": install_brush, "openmvs": install_openmvs}

_lock = threading.Lock()
_state: dict = {"running": False, "tool": "", "message": "", "done": 0, "total": 0,
                "installed": [], "errors": {}}


def install_state() -> dict:
    """Snapshot of the background install (``running`` is False when idle)."""
    with _lock:
        return {**_state, "installed": list(_state["installed"]), "errors": dict(_state["errors"])}


def _run(tools: tuple[str, ...]) -> None:
    for tool in tools:
        def progress(message: str, done: int, total: int, _tool: str = tool) -> None:
            with _lock:
                _state.update(tool=_tool, message=message, done=done, total=total)
        try:
            _INSTALLERS[tool](progress)
            with _lock:
                _state["installed"].append(tool)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, never raised
            with _lock:
                _state["errors"][tool] = str(exc) or type(exc).__name__
    with _lock:
        _state.update(running=False, tool="", message="", done=0, total=0)


def start_install(tools: Optional[list[str]] = None) -> dict:
    """Start installing the requested missing tools (default: every missing one).

    Idempotent while a run is in flight. Returns the state snapshot; poll ``install_state``
    (or ``reveng_status().install``) for progress.
    """
    from services.vision.reveng_provision import _artifact_available
    wanted = tuple(t for t in (tools or TOOLS) if t in TOOLS)
    unknown = [t for t in (tools or []) if t not in TOOLS]
    if unknown:
        raise ValueError(f"Unknown tool(s) {unknown}; choose from {list(TOOLS)}.")
    missing = tuple(t for t in wanted if not _artifact_available(t))
    with _lock:
        if _state["running"] or not missing:
            return {**_state, "installed": list(_state["installed"]), "errors": dict(_state["errors"])}
        _state.update(running=True, tool=missing[0], message="Starting", done=0, total=0,
                      installed=[], errors={})
    threading.Thread(target=_run, args=(missing,), name="reveng-install", daemon=True).start()
    return install_state()
