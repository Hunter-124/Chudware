"""Provision Renode for firmware emulation (engine_oracle/firmware).

Renode is MIT-licensed and driven as a **subprocess only** (never linked / vendored
into the C++ core — contracts/licenses.md). Discovery is PATH-first; known install
roots and ``AICAD_RENODE`` / ``RENODE_ROOT`` are also accepted.

Usage:
    python -m tools.provision_renode            # report what's detected
    python -m tools.provision_renode --install  # fetch portable release (Linux/macOS/Windows)

Install layout (``--install``, user-space, no sudo):
    Linux:   ~/.local/opt/renode  + symlink ~/.local/bin/renode
             (also mirrored under ~/.local/share/AI-CAD/renode for legacy docs)
    macOS:   ~/.local/opt/renode  + symlink ~/.local/bin/renode
    Windows: %LOCALAPPDATA%\\Programs\\renode  (portable zip) — put renode.exe on PATH

Manual options (all auto-detected afterwards):
    * unpack a Renode portable tarball/zip anywhere and put ``renode`` on PATH
    * set AICAD_RENODE or RENODE_ROOT to the install root (dir containing the binary)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

GITHUB_API = "https://api.github.com/repos/renode/renode/releases/latest"
GITHUB_UA = "billet-provision-renode/1.0"

# Well-known roots probed when PATH does not have `renode`.
_KNOWN_ROOTS_POSIX = (
    Path.home() / ".local" / "opt" / "renode",
    Path.home() / ".local" / "share" / "AI-CAD" / "renode",
    Path.home() / ".local" / "share" / "Billet" / "renode",
    Path("/opt/renode"),
    Path("/usr/local/opt/renode"),
)
_KNOWN_ROOTS_WIN = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "renode",
    Path(os.environ.get("APPDATA", "")) / "AI-CAD" / "renode",
    Path(os.environ.get("APPDATA", "")) / "Billet" / "renode",
    Path("C:/Program Files/Renode"),
    Path("C:/renode"),
)

_BINARY_NAMES = ("renode.exe", "renode") if os.name == "nt" else ("renode",)


def _env_root() -> Optional[Path]:
    for key in ("AICAD_RENODE", "RENODE_ROOT"):
        raw = os.environ.get(key)
        if raw:
            p = Path(os.path.expanduser(os.path.expandvars(raw)))
            if p.exists():
                return p
    return None


def _binary_in(root: Path) -> Optional[Path]:
    """Return the renode binary under *root* if present."""
    if root.is_file() and root.name in _BINARY_NAMES:
        return root
    for name in _BINARY_NAMES:
        cand = root / name
        if cand.is_file() and os.access(cand, os.X_OK if os.name != "nt" else os.F_OK):
            return cand
        # nested one level (some archives unpack to renode-x.y.z/)
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir():
                    nested = child / name
                    if nested.is_file():
                        return nested
    return None


def find_renode() -> Optional[Path]:
    """Locate a usable ``renode`` binary. Never raises."""
    which = shutil.which("renode") or shutil.which("renode.exe")
    if which:
        return Path(which)

    env = _env_root()
    if env is not None:
        hit = _binary_in(env)
        if hit is not None:
            return hit

    roots = _KNOWN_ROOTS_WIN if os.name == "nt" else _KNOWN_ROOTS_POSIX
    for root in roots:
        if not root:
            continue
        try:
            if root.exists():
                hit = _binary_in(root)
                if hit is not None:
                    return hit
        except OSError:
            continue
    return None


def probe_version(binary: Path) -> Optional[str]:
    """Best-effort ``renode --version`` short string. Never raises."""
    try:
        r = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=15,
        )
        text = (r.stdout or "") + (r.stderr or "")
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line[:120]
    except Exception:  # noqa: BLE001
        return None
    return None


def status() -> dict:
    """Return a structured availability report (never raises)."""
    binary = find_renode()
    if binary is None:
        return {
            "tool": "renode",
            "installed": False,
            "path": None,
            "version": None,
            "hint": (
                "Install Renode portable from https://renode.io/#downloads "
                "(or run: python -m tools.provision_renode --install)"
            ),
        }
    return {
        "tool": "renode",
        "installed": True,
        "path": str(binary),
        "version": probe_version(binary),
        "hint": None,
    }


def _platform_asset_name(assets: list[dict]) -> Optional[dict]:
    """Pick the best portable asset for this host from a GitHub release assets list."""
    sys_name = platform.system().lower()  # linux / darwin / windows
    machine = platform.machine().lower()  # x86_64 / amd64 / aarch64 / arm64
    names = [a.get("name", "") for a in assets]

    def pick(*needles: str) -> Optional[dict]:
        for a in assets:
            n = a.get("name", "").lower()
            if all(nd in n for nd in needles):
                return a
        return None

    if sys_name == "linux":
        # Prefer classic mono portable (widest compatibility); fall back to dotnet.
        if "aarch64" in machine or "arm64" in machine:
            return (
                pick("linux-arm64", "portable")
                or pick("linux", "arm64", "portable")
                or pick("linux-portable")
            )
        return (
            pick("linux-portable.tar.gz")  # classic mono, not -dotnet
            or next(
                (a for a in assets
                 if "linux-portable" in a.get("name", "").lower()
                 and "dotnet" not in a.get("name", "").lower()
                 and a.get("name", "").endswith(".tar.gz")),
                None,
            )
            or pick("linux-portable-dotnet.tar.gz")
            or pick("linux-portable")
        )

    if sys_name == "darwin":
        if "arm" in machine:
            return pick("osx-arm64") or pick("portable.dmg") or pick(".dmg")
        return pick("renode_") and pick(".dmg") or pick(".dmg")

    if sys_name == "windows" or os.name == "nt":
        return (
            pick("windows-portable", ".zip")
            or pick("windows-portable-dotnet.zip")
            or pick("windows-portable")
            or pick(".setup.exe")
        )

    # Unknown platform — prefer any portable tarball.
    for a in assets:
        n = a.get("name", "").lower()
        if "portable" in n and (n.endswith(".tar.gz") or n.endswith(".zip")):
            return a
    _ = names  # silence unused in some branches
    return None


def _latest_release_asset() -> tuple[str, dict]:
    """Return (tag, asset_dict) for the latest Renode release portable for this host."""
    req = urllib.request.Request(
        GITHUB_API,
        headers={"User-Agent": GITHUB_UA, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name") or data.get("name") or "unknown"
    assets = data.get("assets") or []
    asset = _platform_asset_name(assets)
    if asset is None:
        raise RuntimeError(
            f"no portable Renode asset for {platform.system()}/{platform.machine()} "
            f"in release {tag}; see https://github.com/renode/renode/releases"
        )
    return tag, asset


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": GITHUB_UA})
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _extract_archive(archive: Path, dest_dir: Path) -> Path:
    """Extract *archive* into *dest_dir*; return the directory that contains the binary."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest_dir)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            # Python 3.12+ has filter=; tolerate older.
            try:
                tf.extractall(dest_dir, filter="data")
            except TypeError:
                tf.extractall(dest_dir)
    else:
        raise RuntimeError(f"unsupported archive format: {archive.name}")

    hit = _binary_in(dest_dir)
    if hit is not None:
        # If binary is nested one level, prefer promoting that layout to dest_dir root
        # only when dest_dir itself has no binary.
        return hit.parent
    raise RuntimeError(f"extracted archive but found no renode binary under {dest_dir}")


def _install_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "Programs" / "renode"
    return Path.home() / ".local" / "opt" / "renode"


def _ensure_path_symlink(binary: Path) -> None:
    """Symlink/copy renode onto a user PATH location (posix: ~/.local/bin)."""
    if os.name == "nt":
        # On Windows the install dir itself should be added to user PATH by the
        # installer.ps1 path; we still copy renode.exe next to itself (noop).
        return
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "renode"
    try:
        if link.is_symlink() or link.exists():
            if link.resolve() == binary.resolve():
                return
            link.unlink()
        link.symlink_to(binary)
        print(f"[provision] linked {link} -> {binary}")
    except OSError as exc:
        # Fall back to a small wrapper script if symlink fails (e.g. FS no-symlink).
        print(f"[provision] symlink failed ({exc}); writing wrapper at {link}")
        link.write_text(
            f"#!/bin/sh\nexec '{binary}' \"$@\"\n",
            encoding="utf-8",
        )
        link.chmod(0o755)


def install() -> bool:
    """Download + extract the latest portable Renode release. Returns True on success."""
    existing = find_renode()
    if existing is not None:
        ver = probe_version(existing) or "unknown"
        print(f"[provision] Renode already available: {existing} ({ver})")
        _ensure_path_symlink(existing)
        return True

    print("[provision] resolving latest Renode release via GitHub API...")
    try:
        tag, asset = _latest_release_asset()
    except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"[provision] failed to resolve release: {exc}")
        print("            Manual: download portable from https://renode.io/#downloads")
        return False

    url = asset.get("browser_download_url")
    name = asset.get("name", "renode-portable")
    if not url:
        print("[provision] release asset has no browser_download_url")
        return False

    root = _install_root()
    print(f"[provision] installing Renode {tag} ({name}) -> {root}")
    try:
        with tempfile.TemporaryDirectory(prefix="renode-prov-") as tmp:
            archive = Path(tmp) / name
            print(f"[provision] downloading {url} ...")
            _download(url, archive)
            # Stage into a temp dir, then swap into root for atomic-ish replace.
            stage = Path(tmp) / "stage"
            stage.mkdir()
            bin_parent = _extract_archive(archive, stage)
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            root.parent.mkdir(parents=True, exist_ok=True)
            # If binary lives at stage/renode, move stage -> root; if nested, move that.
            if bin_parent.resolve() == stage.resolve():
                shutil.move(str(stage), str(root))
            else:
                shutil.move(str(bin_parent), str(root))
    except Exception as exc:  # noqa: BLE001
        print(f"[provision] install failed: {exc}")
        return False

    binary = _binary_in(root)
    if binary is None:
        print(f"[provision] install finished but no binary under {root}")
        return False

    # Ensure executable bit on posix.
    if os.name != "nt":
        try:
            binary.chmod(binary.stat().st_mode | 0o111)
        except OSError:
            pass

    _ensure_path_symlink(binary)

    # Legacy mirror path used by older demo docs.
    if os.name != "nt":
        legacy = Path.home() / ".local" / "share" / "AI-CAD" / "renode"
        try:
            if not legacy.exists():
                legacy.parent.mkdir(parents=True, exist_ok=True)
                # Prefer symlink of the whole tree for disk savings.
                try:
                    legacy.symlink_to(root, target_is_directory=True)
                except OSError:
                    pass
        except OSError:
            pass

    ver = probe_version(binary) or "unknown"
    print(f"[provision] Renode installed: {binary} ({ver})")
    return True


def report() -> int:
    """Print the detected Renode; exit 0 if reachable, else 1."""
    st = status()
    if st["installed"]:
        print(f"[provision] Renode detected: path={st['path']}  version={st['version']}")
        return 0
    print("[provision] No Renode detected on this host.")
    if os.name == "posix":
        print("            Run with --install for a user-space portable tarball (no sudo),")
        print("            or unpack a release under ~/.local/opt/renode and put renode on PATH,")
        print("            or set AICAD_RENODE to an install root.")
    else:
        print("            Run with --install for the Windows portable zip,")
        print("            or install from https://renode.io/#downloads and put renode.exe on PATH,")
        print("            or set %AICAD_RENODE% to the install root.")
    print(f"            hint: {st['hint']}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Provision/detect Renode for firmware emulation."
    )
    ap.add_argument(
        "--install",
        action="store_true",
        help="download portable Renode release into a user-space prefix if not already present",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit status() as JSON (implies report-only; exit code still reflects presence)",
    )
    args = ap.parse_args(argv)

    if args.install:
        if find_renode() is not None:
            print("[provision] Renode already available — nothing to do.")
        else:
            install()

    if args.json:
        st = status()
        print(json.dumps(st, indent=2))
        return 0 if st["installed"] else 1
    return report()


if __name__ == "__main__":
    sys.exit(main())
