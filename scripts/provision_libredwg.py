#!/usr/bin/env python3
"""Install the pinned LibreDWG release (GPL-3.0) for DWG import.

Chudware reads the 2D entities of a DWG with LibreDWG's ``dwg2dxf``; it writes DWG with its
own native encoder (core/doc/dwg_write.cpp) and needs no LibreDWG for that. ``dwg2dxf`` runs
strictly as a subprocess (never linked or bundled; contracts/licenses.md). This script
fetches the SHA256-pinned upstream release onto the user's own machine:

* Linux/macOS: builds the source tarball (static, no bindings/docs) with the host toolchain.
* Windows: unpacks the upstream win64 binary zip.

The result lands in ``<appdata>/Chudware/tools/libredwg/bin``, which both the C++ core and
the Python helpers search after ``PATH``. A distro/Homebrew ``libredwg`` on PATH needs no
provisioning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.formats.libredwg import (  # noqa: E402
    LIBREDWG_REPOSITORY, LIBREDWG_SOURCE_SHA256, LIBREDWG_SOURCE_URL, LIBREDWG_VERSION,
    LIBREDWG_WIN64_SHA256, LIBREDWG_WIN64_URL, install_dir,
)

# tool -> location inside the built source tree.
TOOLS = {"dwg2dxf": "programs"}
_DOWNLOAD_LIMIT = 512 * 1024 ** 2
_EXTRACT_LIMIT = 2 * 1024 ** 3


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _download(url: str, sha256: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Chudware-LibreDWG-Provisioner/1"})
    checksum = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, dest.open("xb") as out:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > _DOWNLOAD_LIMIT:
                raise ValueError("LibreDWG release exceeds the download limit")
            checksum.update(chunk)
            out.write(chunk)
    if checksum.hexdigest() != sha256:
        raise ValueError(f"LibreDWG release SHA256 mismatch: expected {sha256}, "
                         f"received {checksum.hexdigest()}")


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {name}")
    return path


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:xz") as bundle:
        total = 0
        for member in bundle.getmembers():
            _safe_member(member.name)
            if not (member.isfile() or member.isdir()):
                continue  # source trees carry no links we need; never follow any
            total += member.size
            if total > _EXTRACT_LIMIT:
                raise ValueError("LibreDWG source exceeds the extraction limit")
            bundle.extract(member, dest, filter="data")


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        total = 0
        for info in bundle.infolist():
            rel = _safe_member(info.filename)
            total += info.file_size
            if total > _EXTRACT_LIMIT:
                raise ValueError("LibreDWG binaries exceed the extraction limit")
            target = dest.joinpath(*rel.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as src, target.open("xb") as out:
                shutil.copyfileobj(src, out)


def _build_from_source(work: Path, payload: Path) -> None:
    for prog in ("make", "cc"):
        if shutil.which(prog) is None and not (prog == "cc" and shutil.which("gcc")):
            raise ValueError(f"Building LibreDWG needs '{prog}' on PATH (install a C toolchain).")
    archive = work / "libredwg.tar.xz"
    _download(LIBREDWG_SOURCE_URL, LIBREDWG_SOURCE_SHA256, archive)
    src_root = work / "src"
    src_root.mkdir()
    _extract_tar(archive, src_root)
    source = src_root / f"libredwg-{LIBREDWG_VERSION}"
    if not (source / "configure").is_file():
        raise ValueError(f"LibreDWG source tree missing configure: {source}")
    # Static tools (no shared libredwg, no bindings/docs) are relocatable, so the build
    # prefix is the final location only nominally; the payload is renamed into place.
    subprocess.run(["./configure", f"--prefix={payload}", "--disable-shared", "--enable-static",
                    "--disable-bindings", "--disable-docs"], cwd=source, check=True)
    jobs = str(max(1, os.cpu_count() or 1))
    subprocess.run(["make", f"-j{jobs}"], cwd=source, check=True)
    bin_dir = payload / "bin"
    bin_dir.mkdir(parents=True)
    for name, subdir in TOOLS.items():
        built = source / subdir / name
        if not built.is_file():
            raise ValueError(f"LibreDWG build produced no {name}")
        shutil.copy2(built, bin_dir / name)
        (bin_dir / name).chmod(0o755)
    shutil.copy2(source / "COPYING", payload / "COPYING")


def _unpack_windows(work: Path, payload: Path) -> None:
    archive = work / "libredwg-win64.zip"
    _download(LIBREDWG_WIN64_URL, LIBREDWG_WIN64_SHA256, archive)
    unpacked = work / "unpacked"
    unpacked.mkdir()
    _extract_zip(archive, unpacked)
    hits = list(unpacked.rglob(_exe("dwg2dxf")))
    if len(hits) != 1:
        raise ValueError("LibreDWG win64 zip must contain exactly one dwg2dxf.exe")
    # The tools ship beside their DLLs; keep the whole directory together as bin/.
    bin_dir = payload / "bin"
    hits[0].parent.rename(bin_dir)
    for name, subdir in TOOLS.items():
        nested = bin_dir / subdir / _exe(name)
        if not (bin_dir / _exe(name)).is_file() and nested.is_file():
            shutil.copy2(nested, bin_dir / _exe(name))
        if not (bin_dir / _exe(name)).is_file():
            raise ValueError(f"LibreDWG win64 zip has no {_exe(name)}")


def _verified(target: Path) -> bool:
    try:
        receipt = json.loads((target / "install.json").read_text())
        return (receipt["version"] == LIBREDWG_VERSION
                and all(receipt["tools"][n] == _digest(target / "bin" / _exe(n)) for n in TOOLS))
    except (OSError, ValueError, KeyError):
        return False


def install(force: bool = False) -> Path:
    target = install_dir()
    if target.exists():
        if _verified(target) and not force:
            return target / "bin"
        if not force:
            raise ValueError(f"Existing install is not a verified LibreDWG {LIBREDWG_VERSION}: "
                             f"{target}. Re-run with --force to replace it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".libredwg-install-", dir=target.parent) as tmp:
        work = Path(tmp)
        payload = work / "payload"
        payload.mkdir()
        if platform.system() == "Windows":
            _unpack_windows(work, payload)
            url, sha = LIBREDWG_WIN64_URL, LIBREDWG_WIN64_SHA256
        else:
            _build_from_source(work, payload)
            url, sha = LIBREDWG_SOURCE_URL, LIBREDWG_SOURCE_SHA256
        for name in TOOLS:
            probe = subprocess.run([str(payload / "bin" / _exe(name)), "--version"],
                                   capture_output=True, text=True, timeout=30)
            if probe.returncode != 0:
                raise ValueError(f"{name} --version failed: {probe.stderr.strip()}")
        receipt = {"version": LIBREDWG_VERSION, "license": "GPL-3.0-or-later",
                   "source_repository": LIBREDWG_REPOSITORY, "archive_url": url,
                   "archive_sha256": sha,
                   "tools": {n: _digest(payload / "bin" / _exe(n)) for n in TOOLS}}
        (payload / "install.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if target.exists():
            retired = work / "retired"
            target.rename(retired)
        payload.rename(target)
    return target / "bin"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Install LibreDWG {LIBREDWG_VERSION} (GPL-3.0, subprocess-only) for "
                    "Chudware DWG import/export.",
        epilog="A libredwg already on PATH takes precedence at runtime; this install is the "
               "fallback. XDG_DATA_HOME / APPDATA select the managed location.")
    parser.add_argument("--force", action="store_true",
                        help="Replace an existing managed install.")
    args = parser.parse_args()
    try:
        bin_dir = install(force=args.force)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"LibreDWG provisioning failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified LibreDWG {LIBREDWG_VERSION} tools: {bin_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
