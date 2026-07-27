#!/usr/bin/env python3
"""Deterministically vendor a whitelisted subset of oh-my-pi ("OMP") into vendor/omp/.

We reuse OMP subsystems (providers, memory, snapcompact) verbatim by importing OMP's real
TypeScript from a bun "bridge" process. This script is the *update* mechanism: it pins an
upstream commit, copies only the packages we recycle, regenerates a minimal bun workspace
root, and records a content-hash lockfile so re-vendoring is reproducible and reviewable.

Design:
  vendor/omp/
    packages/<pkg>/     <- vendored OMP source (CLOBBERED on every update)
    bridge/             <- OUR bun bridge package (PRESERVED; never touched here)
    package.json        <- generated minimal workspace root (workspaces: packages/* + bridge)
    bunfig.toml, tsconfig*.json, patches/   <- copied from upstream (needed by bun install)
    OMP_VENDOR.lock     <- {source, sha, packages, files:{path:sha256}} for reproducibility

Usage:
  # initial pin from the local inspection checkout (already at the pinned SHA), offline:
  python3 scripts/vendor_omp.py --from scratch/omp-src
  # pull a newer upstream commit (the "keep getting OMP goodies" path):
  python3 scripts/vendor_omp.py --from https://github.com/can1357/oh-my-pi.git --sha <git-sha>
  # verify the working tree still matches the lockfile (CI / pre-build gate):
  python3 scripts/vendor_omp.py --verify

Exit non-zero on any failure so build pipelines catch drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Packages we recycle from OMP, plus their transitive workspace deps. Keep this list tight
# (YAGNI): it is the whole point of the vendoring — only pull the parts we actually reuse.
#   pi-ai        -> pi-catalog, pi-utils, pi-wire         (providers + streaming)
#   pi-catalog   -> pi-utils                              (model metadata)
#   pi-utils     -> pi-natives (npm)                      (shared utils)
#   pi-wire                                               (leaf)
#   pi-mnemopi   -> pi-ai, pi-catalog, pi-utils           (long-term memory)
#   snapcompact  -> pi-ai, pi-natives (npm), pi-utils, pi-wire  (image-tier compaction)
#
# pi-natives is deliberately NOT vendored as source: it is a native Rust/N-API addon whose
# .node binary is a gitignored build artifact. OMP publishes prebuilt per-platform packages
# (@oh-my-pi/pi-natives-{linux,darwin,win32}-{x64,arm64}) to npm; pi-utils/snapcompact depend
# on it via `catalog:`, so `bun install` fetches the correct prebuilt binary for the host —
# no Rust toolchain, clean cross-platform parity (the catalog pins the version in package.json).
DEFAULT_PACKAGES = [
    "ai",
    "catalog",
    "utils",
    "wire",
    "mnemopi",
    "snapcompact",
]

# Root workspace metadata copied verbatim from upstream (needed for `bun install` + TS resolution).
ROOT_FILES = ["bunfig.toml", "tsconfig.base.json", "tsconfig.json"]
ROOT_DIRS = ["patches"]

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "vendor" / "omp"
LOCK_PATH = VENDOR_DIR / "OMP_VENDOR.lock"
DEFAULT_UPSTREAM = "https://github.com/can1357/oh-my-pi.git"

# Directories inside a package we never need at runtime (tests, fixtures) — trims the vendored
# footprint without affecting imports. Conservative: only drop clearly non-runtime dirs.
PRUNE_DIRS = {"test", "tests", "__tests__", "bench", "benchmarks", "node_modules", ".turbo"}


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}")
    return res.stdout.strip()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def _resolve_source(from_arg: str, sha: str | None) -> tuple[Path, str, str, list[Path]]:
    """Return (source_root, source_id, resolved_sha, cleanup_paths)."""
    cleanup: list[Path] = []
    p = Path(from_arg).expanduser()
    if p.exists() and (p / "packages").is_dir():
        # Local checkout. Read its HEAD sha when it is a git repo.
        try:
            resolved = _run(["git", "-C", str(p), "rev-parse", "HEAD"])
        except SystemExit:
            resolved = sha or "unknown"
        return p.resolve(), str(p.resolve()), resolved, cleanup
    # Treat as a git URL: shallow-fetch the requested SHA into a temp dir.
    if not sha:
        raise SystemExit("--sha is required when --from is a git URL")
    tmp = Path(tempfile.mkdtemp(prefix="omp-vendor-"))
    cleanup.append(tmp)
    _run(["git", "init", "-q", str(tmp)])
    _run(["git", "-C", str(tmp), "remote", "add", "origin", from_arg])
    _run(["git", "-C", str(tmp), "fetch", "-q", "--depth", "1", "origin", sha])
    _run(["git", "-C", str(tmp), "checkout", "-q", "FETCH_HEAD"])
    return tmp, from_arg, sha, cleanup


def _copytree_pruned(src: Path, dst: Path) -> None:
    def ignore(dirpath, names):
        return [n for n in names if n in PRUNE_DIRS]
    shutil.copytree(src, dst, ignore=ignore)


def _generate_root_package_json(source_root: Path) -> dict:
    """Minimal bun workspace root: only the vendored packages + our preserved bridge/."""
    upstream = json.loads((source_root / "package.json").read_text(encoding="utf-8"))
    catalog = upstream.get("workspaces", {}).get("catalog", {})
    root = {
        "name": "cad-omp-vendor",
        "private": True,
        "type": "module",
        "packageManager": upstream.get("packageManager", "bun@1.3.14"),
        # bridge/ is OUR package and is preserved across re-vendors.
        "workspaces": {
            "packages": ["packages/*", "bridge"],
            # Reuse upstream's dependency catalog verbatim (our packages pin via `catalog:`).
            "catalog": catalog,
        },
    }
    # Keep only patch entries whose patch file we actually carry (avoids bun errors on
    # patches for packages outside our closure).
    patched = upstream.get("patchedDependencies", {})
    kept = {}
    for spec, rel in patched.items():
        if (source_root / rel).exists():
            kept[spec] = rel
    if kept:
        root["patchedDependencies"] = kept
    return root


OMP_SCOPE = "@oh-my-pi/"

# pi-natives' per-platform prebuilt binaries ride along as optionalDependencies of the
# npm package (not the workspace catalog), so they must be exempted by name as well.
PI_NATIVES_PLATFORM_PKGS = [
    f"{OMP_SCOPE}pi-natives-{os}-{arch}"
    for os in ("linux", "darwin", "win32")
    for arch in ("x64", "arm64")
]


def _patch_bunfig(path: Path, catalog: dict) -> None:
    """Exempt our `@oh-my-pi/*` catalog pins from upstream's `minimumReleaseAge` gate.

    The gate is a supply-chain cooldown for floating ranges; our OMP deps are exactly
    pinned (`exact = true` + catalog pin to the vendored release), so the cooldown only
    blocks re-vendoring any upstream release younger than 3 days — precisely when a
    re-vendor happens (fresh upstream fixes). Rewrites the copied bunfig in place.
    """
    if not path.exists():
        return
    omp_pkgs = sorted(k for k in catalog if k.startswith(OMP_SCOPE))
    if not omp_pkgs:
        return
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^minimumReleaseAgeExcludes = \[(.*?)\]", text, flags=re.M)
    existing: list[str] = []
    if m:
        existing = re.findall(r'"([^"]+)"', m.group(1))
    merged = sorted(set(existing) | set(omp_pkgs) | set(PI_NATIVES_PLATFORM_PKGS))
    line = "minimumReleaseAgeExcludes = [" + ", ".join(f'"{p}"' for p in merged) + "]"
    if m:
        text = text[: m.start()] + line + text[m.end() :]
    elif re.search(r"^\[install\]", text, flags=re.M):
        text = re.sub(r"^\[install\]\n", "[install]\n" + line + "\n", text, count=1, flags=re.M)
    else:
        text += "\n[install]\n" + line + "\n"
    path.write_text(text, encoding="utf-8")


def _excluded_from_lock(rel: str) -> bool:
    """Paths the content-hash lock must not pin: our preserved bridge/, install output
    (node_modules at any depth, the bun.lock `bun install` regenerates), and the lock itself."""
    return (
        rel.startswith("bridge/")
        or rel.startswith("node_modules/")
        or "/node_modules/" in rel
        or rel == "bun.lock"
        or rel == "OMP_VENDOR.lock"
    )


def vendor(from_arg: str, sha: str | None, packages: list[str], run_install: bool) -> None:
    source_root, source_id, resolved_sha, cleanup = _resolve_source(from_arg, sha)
    try:
        pkgs_src = source_root / "packages"
        for pkg in packages:
            if not (pkgs_src / pkg).is_dir():
                raise SystemExit(f"package '{pkg}' not found under {pkgs_src}")

        # Preserve our bridge/ across updates; clobber everything else under vendor/omp.
        bridge_backup = None
        if (VENDOR_DIR / "bridge").exists():
            bridge_backup = Path(tempfile.mkdtemp(prefix="omp-bridge-"))
            shutil.copytree(VENDOR_DIR / "bridge", bridge_backup / "bridge")
            cleanup.append(bridge_backup)
        if VENDOR_DIR.exists():
            shutil.rmtree(VENDOR_DIR)
        (VENDOR_DIR / "packages").mkdir(parents=True)

        for pkg in packages:
            _copytree_pruned(pkgs_src / pkg, VENDOR_DIR / "packages" / pkg)

        for f in ROOT_FILES:
            if (source_root / f).exists():
                shutil.copy2(source_root / f, VENDOR_DIR / f)
        for d in ROOT_DIRS:
            if (source_root / d).is_dir():
                shutil.copytree(source_root / d, VENDOR_DIR / d)

        root_pkg = _generate_root_package_json(source_root)
        _patch_bunfig(
            VENDOR_DIR / "bunfig.toml",
            root_pkg.get("workspaces", {}).get("catalog", {}),
        )
        (VENDOR_DIR / "package.json").write_text(
            json.dumps(root_pkg, indent=2) + "\n",
            encoding="utf-8",
        )

        if bridge_backup is not None:
            shutil.copytree(bridge_backup / "bridge", VENDOR_DIR / "bridge")

        # Content-hash manifest (exclude our bridge/ and any node_modules/lock churn).
        manifest: dict[str, str] = {}
        for p in _iter_files(VENDOR_DIR):
            rel = p.relative_to(VENDOR_DIR).as_posix()
            if _excluded_from_lock(rel):
                continue
            manifest[rel] = _sha256_file(p)
        lock = {
            "source": source_id,
            "sha": resolved_sha,
            "packages": packages,
            "file_count": len(manifest),
            "files": manifest,
        }
        LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")

        print(f"vendored {len(packages)} packages from {source_id} @ {resolved_sha[:12]}")
        print(f"  {len(manifest)} files -> {VENDOR_DIR.relative_to(REPO_ROOT)}")

        if run_install:
            bun = shutil.which("bun")
            if not bun:
                raise SystemExit("bun not found on PATH; cannot --install")
            print("running `bun install` in vendored workspace...")
            _run([bun, "install"], cwd=VENDOR_DIR)
            print("bun install OK")
    finally:
        for c in cleanup:
            shutil.rmtree(c, ignore_errors=True)


def verify() -> int:
    if not LOCK_PATH.exists():
        print("no OMP_VENDOR.lock — run vendoring first", file=sys.stderr)
        return 1
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    drift = []
    for rel, want in lock["files"].items():
        p = VENDOR_DIR / rel
        if not p.exists():
            drift.append(f"missing: {rel}")
        elif _sha256_file(p) != want:
            drift.append(f"changed: {rel}")
    # New tracked files (excluding bridge/ + install output) that are not in the lock.
    for p in _iter_files(VENDOR_DIR):
        rel = p.relative_to(VENDOR_DIR).as_posix()
        if _excluded_from_lock(rel):
            continue
        if rel not in lock["files"]:
            drift.append(f"untracked: {rel}")
    if drift:
        print(f"VENDOR DRIFT ({len(drift)}):", file=sys.stderr)
        for d in drift[:50]:
            print("  " + d, file=sys.stderr)
        return 1
    print(f"vendor OK: {lock['file_count']} files match {lock['sha'][:12]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="from_arg", default=DEFAULT_UPSTREAM,
                    help="local OMP checkout path or git URL (default: upstream)")
    ap.add_argument("--sha", default=None, help="upstream commit SHA (required for git URL)")
    ap.add_argument("--packages", nargs="*", default=DEFAULT_PACKAGES,
                    help="package names under packages/ to vendor")
    ap.add_argument("--install", action="store_true", help="run `bun install` after vendoring")
    ap.add_argument("--verify", action="store_true", help="check working tree against the lock")
    args = ap.parse_args()
    if args.verify:
        return verify()
    vendor(args.from_arg, args.sha, args.packages, args.install)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
