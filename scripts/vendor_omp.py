#!/usr/bin/env python3
"""Deterministically vendor a whitelisted subset of Hunter's private OMP platform into
vendor/omp/.

CAD reuses OMP subsystems (providers, memory, snapcompact) verbatim by importing OMP's real
TypeScript from a bun "bridge" process. This script is the *update* mechanism: it pins a
private-harness commit, copies only the packages CAD recycles, regenerates a minimal bun
workspace root, and records a content-hash lockfile so re-vendoring is reproducible and
reviewable.

Design:
  vendor/omp/
    packages/<pkg>/     <- vendored OMP source (CLOBBERED on every update)
    bridge/             <- OUR bun bridge package (PRESERVED; never touched here)
    package.json        <- generated minimal workspace root (workspaces: packages/* + bridge)
    bunfig.toml, tsconfig*.json, patches/   <- copied from upstream (needed by bun install)
    OMP_VENDOR.lock     <- {source, sha, dirty, dirty_files, packages, files:{path:sha256}}

`sha` alone pins the vendored bytes when the source checkout is CLEAN. Dirty source checkouts
remain supported for diagnosis and record `dirty: true`, `dirty_files`, plus a hash-pinned
`local-patches/` series — but production CAD MUST vendor a clean commit from
`Hunter-124/omp`. Local patches were the migration path, not the steady state.

Usage:
  # production refresh from the private AI backbone (default source):
  python3 scripts/vendor_omp.py --sha <Hunter-124/omp git sha>
  # inspect a clean local private-harness checkout offline:
  python3 scripts/vendor_omp.py --from /home/hunter/Repositories/omp
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
DEFAULT_UPSTREAM = "https://github.com/Hunter-124/omp.git"

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


def _local_modifications(root: Path) -> list[str]:
    """Paths in the source checkout that differ from its own HEAD, as git reports them.

    A non-empty result means the vendored bytes are NOT reproducible from the recorded
    `sha` alone: they are upstream plus uncommitted local work (our fork's patches live
    exactly there). Untracked files count — they get copied into vendor/ just the same.
    Returns [] when the source is not a git checkout (nothing to compare against) or for
    the git-URL path, whose temp clone is clean by construction.
    """
    res = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    # `-z` records are NUL-separated "XY path" (note: NOT stripped — an unstaged-only change
    # reports a LEADING SPACE in the code, so the path always starts at index 3).
    # Rename/copy records append the source path as a second record; it changed locally too.
    fields = [f for f in res.stdout.split("\0") if f]
    mods: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in ("R", "C") and i < len(fields):
            mods.append(fields[i])
            i += 1
        mods.append(entry[3:])
    return sorted(set(mods))


def _git_ignored(rels: list[str]) -> set[str]:
    """Subset of vendor-relative paths that git itself ignores.

    `--verify` must not cry drift over per-machine artifacts the repo deliberately does not
    track (the bridge install fingerprint + its lock today, whatever lands beside them
    tomorrow). Asking git instead of hardcoding names keeps that true as .gitignore grows.
    `check-ignore` consults the index, so a TRACKED vendored file is never reported ignored
    and genuine drift still surfaces. Degrades to the old strict behavior (empty set) when
    git is missing or errors.
    """
    if not rels or not shutil.which("git"):
        return set()
    args = [(VENDOR_DIR / rel).relative_to(REPO_ROOT).as_posix() for rel in rels]
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-z", "--stdin"],
        input="\0".join(args),
        capture_output=True,
        text=True,
    )
    # 0 = some ignored, 1 = none ignored; anything else (128) means git could not answer.
    if res.returncode not in (0, 1):
        return set()
    ignored = {line for line in res.stdout.split("\0") if line}
    prefix = VENDOR_DIR.relative_to(REPO_ROOT).as_posix() + "/"
    return {p[len(prefix) :] for p in ignored if p.startswith(prefix)}



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


LOCAL_PATCH_DIRNAME = "local-patches"


def _excluded_from_lock(rel: str) -> bool:
    """Paths the content-hash lock must not pin under `files`, because they are ours rather
    than upstream bytes: our preserved bridge/ and bun.lock, our captured local-patches/,
    install output (node_modules at any depth), and the lock itself. bun.lock is excluded
    from the manifest but PRESERVED across re-vendor; local-patches/ is regenerated from the
    source checkout each time and hash-pinned separately under `local_patches`."""
    return (
        rel.startswith("bridge/")
        or rel.startswith(LOCAL_PATCH_DIRNAME + "/")
        or rel.startswith("node_modules/")
        or "/node_modules/" in rel
        or rel == "bun.lock"
        or rel == "OMP_VENDOR.lock"
    )


def _patch_filename(index: int, rel: str) -> str:
    """Stable, filesystem-safe name for one captured local patch."""
    flat = rel.replace("/", "__")
    return f"{index:02d}-{flat}.patch"


def _capture_local_patches(root: Path, dirty_files: list[str], dest: Path) -> dict[str, str]:
    """Write one unified diff per locally-modified source file into `dest`; return
    {filename: sha256}.

    WHY THIS EXISTS. `dirty_files` records only the NAMES of the local patches our fork
    carries, so the vendored bytes were reproducible only from the same uncommitted working
    tree — a gitignored scratch clone. One `git clean` there and the provider stack's local
    fixes (the OAuth close-window degrade, the SQLite corruption detection) could not be
    rebuilt or even identified, and the next re-vendor would silently drop them while
    `--verify` still passed against whatever was vendored at the time. Capturing the diffs
    into the repo closes that hole: `vendor/omp/local-patches/` is committed, hash-pinned in
    the lock, and enough to reapply the series onto a fresh upstream checkout.

    Tracked files diff against HEAD. Untracked files are captured with `--no-index` against
    /dev/null, which yields a normal add-file patch without touching the source index (this
    must not mutate the caller's checkout).
    """
    dest.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for i, rel in enumerate(sorted(dirty_files), start=1):
        abs_path = root / rel
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel],
            capture_output=True,
            text=True,
        ).returncode == 0
        if tracked:
            cmd = ["git", "-C", str(root), "diff", "HEAD", "--", rel]
        elif abs_path.is_file():
            # `git diff` exits 1 when there IS a difference, which is the normal case here.
            cmd = ["git", "-C", str(root), "diff", "--no-index", "--", "/dev/null", str(abs_path)]
        else:
            # Locally DELETED tracked path already covered by the HEAD diff above; a missing
            # untracked path is nothing to capture.
            continue
        res = subprocess.run(cmd, capture_output=True, text=True)
        body = res.stdout
        if not body.strip():
            continue
        name = _patch_filename(i, rel)
        (dest / name).write_text(body, encoding="utf-8")
        hashes[name] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if hashes:
        readme = (
            "# Local patches over the pinned upstream OMP checkout\n\n"
            "Generated by `scripts/vendor_omp.py`. Each file is a unified diff against the\n"
            "upstream `sha` recorded in `OMP_VENDOR.lock`, in apply order.\n\n"
            "These are ALREADY baked into the vendored bytes under `vendor/omp/packages/`.\n"
            "They are kept here so the vendored tree stays reproducible from this repository\n"
            "alone, instead of only from a gitignored scratch clone's uncommitted working\n"
            "tree. `scripts/vendor_omp.py --verify` hash-checks every file listed below.\n\n"
            "To rebuild the source tree these were vendored from:\n\n"
            "```\n"
            "git clone <upstream> omp-src && cd omp-src\n"
            "git checkout <sha from OMP_VENDOR.lock>\n"
            "for p in <repo>/vendor/omp/local-patches/*.patch; do git apply \"$p\"; done\n"
            "```\n\n"
            "Patches in this series:\n\n"
            + "".join(f"- `{n}`\n" for n in sorted(hashes))
        )
        (dest / "README.md").write_text(readme, encoding="utf-8")
        hashes["README.md"] = hashlib.sha256(readme.encode("utf-8")).hexdigest()
    return hashes


def vendor(from_arg: str, sha: str | None, packages: list[str], run_install: bool) -> None:
    source_root, source_id, resolved_sha, cleanup = _resolve_source(from_arg, sha)
    try:
        pkgs_src = source_root / "packages"
        for pkg in packages:
            if not (pkgs_src / pkg).is_dir():
                raise SystemExit(f"package '{pkg}' not found under {pkgs_src}")

        # Read the OUTGOING lock before the clobber: if it recorded a local patch series and
        # the new source has none, that is a silent provider-stack regression worth shouting
        # about (see the warning at the end of this function).
        prior_patch_names: list[str] = []
        if LOCK_PATH.exists():
            try:
                prior = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior = {}
            prior_patch_names = [
                k for k in (prior.get("local_patches") or {}) if k != "README.md"
            ]

        # Preserve our own artifacts across updates; clobber everything else under vendor/omp.
        # bridge/ is our package. bun.lock is our committed install input: the C++ core
        # materializes node_modules from it with --frozen-lockfile so providers work with no
        # manual `bun install` step (core/agent/omp_bridge_client.cpp). Upstream's lock
        # describes a much larger workspace than our generated root package.json, so it cannot
        # be copied from source — it has to survive the clobber or every re-vendor silently
        # breaks provider auth until someone notices node_modules can no longer be built.
        preserved = Path(tempfile.mkdtemp(prefix="omp-preserve-"))
        cleanup.append(preserved)
        if (VENDOR_DIR / "bridge").exists():
            shutil.copytree(VENDOR_DIR / "bridge", preserved / "bridge")
        if (VENDOR_DIR / "bun.lock").exists():
            shutil.copy2(VENDOR_DIR / "bun.lock", preserved / "bun.lock")
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

        if (preserved / "bridge").exists():
            shutil.copytree(preserved / "bridge", VENDOR_DIR / "bridge")
        if (preserved / "bun.lock").exists():
            shutil.copy2(preserved / "bun.lock", VENDOR_DIR / "bun.lock")

        # Content-hash manifest (exclude our bridge/ and any node_modules/lock churn).
        manifest: dict[str, str] = {}
        for p in _iter_files(VENDOR_DIR):
            rel = p.relative_to(VENDOR_DIR).as_posix()
            if _excluded_from_lock(rel):
                continue
            manifest[rel] = _sha256_file(p)
        dirty_files = _local_modifications(source_root)
        # Capture the local patch series into the repo so the vendored tree is reproducible
        # from this checkout alone, not only from the source working tree.
        local_patches = _capture_local_patches(
            source_root, dirty_files, VENDOR_DIR / LOCAL_PATCH_DIRNAME
        )
        lock = {
            "source": source_id,
            "sha": resolved_sha,
            # Honesty about provenance: `dirty` true => the bytes below are upstream `sha`
            # PLUS the local work listed in `dirty_files`. `files` pins the upstream content;
            # `local_patches` pins the captured diffs that reproduce it from `sha`.
            "dirty": bool(dirty_files),
            "dirty_files": dirty_files,
            "local_patches": local_patches,
            "packages": packages,
            "file_count": len(manifest),
            "files": manifest,
        }
        LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")

        suffix = " (DIRTY source)" if dirty_files else ""
        print(f"vendored {len(packages)} packages from {source_id} @ {resolved_sha[:12]}{suffix}")
        print(f"  {len(manifest)} files -> {VENDOR_DIR.relative_to(REPO_ROOT)}")
        for rel in dirty_files:
            print(f"  local patch (not in {resolved_sha[:12]}): {rel}")
        if local_patches:
            n = len([k for k in local_patches if k != "README.md"])
            print(f"  captured {n} patch file(s) -> "
                  f"{(VENDOR_DIR / LOCAL_PATCH_DIRNAME).relative_to(REPO_ROOT)}")
        elif prior_patch_names:
            # The source went clean while the repo still carried a captured series. Vendoring
            # from a fresh clone that lost the patches would quietly regress the provider
            # stack, and the new lock would look perfectly healthy, so say it loudly.
            print(
                f"  WARNING: the previous lock recorded {len(prior_patch_names)} local "
                f"patch file(s) and this source has none.\n"
                f"           If that is not deliberate, STOP: the vendored bytes just lost "
                f"local fixes.\n"
                f"           Previously recorded: {', '.join(sorted(prior_patch_names))}"
            )

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
    # Files present under vendor/omp but absent from the lock. Paths git ignores are not
    # drift: they are per-machine artifacts the repo intentionally does not track.
    extra = []
    for p in _iter_files(VENDOR_DIR):
        rel = p.relative_to(VENDOR_DIR).as_posix()
        if _excluded_from_lock(rel) or rel in lock["files"]:
            continue
        extra.append(rel)
    ignored = _git_ignored(extra)
    drift += [f"untracked: {rel}" for rel in extra if rel not in ignored]
    # The captured local patch series is excluded from `files` (it is ours, not upstream
    # bytes) but is still hash-pinned, because it is the ONLY record of how to reproduce the
    # vendored tree from the recorded upstream sha. An unnoticed deletion here would leave
    # the provider stack's local fixes unreconstructible.
    for name, want in (lock.get("local_patches") or {}).items():
        p = VENDOR_DIR / LOCAL_PATCH_DIRNAME / name
        if not p.exists():
            drift.append(f"missing local patch: {LOCAL_PATCH_DIRNAME}/{name}")
        elif _sha256_file(p) != want:
            drift.append(f"changed local patch: {LOCAL_PATCH_DIRNAME}/{name}")
    if drift:
        print(f"VENDOR DRIFT ({len(drift)}):", file=sys.stderr)
        for d in drift[:50]:
            print("  " + d, file=sys.stderr)
        return 1
    # `dirty`/`dirty_files` are absent from pre-existing locks; treat that as clean.
    note = ""
    if lock.get("dirty"):
        note = f" + {len(lock.get('dirty_files', []))} local patch(es)"
    patch_files = [k for k in (lock.get("local_patches") or {}) if k != "README.md"]
    if patch_files:
        note += f" ({len(patch_files)} captured, hash-verified)"
    print(f"vendor OK: {lock['file_count']} files match {lock['sha'][:12]}{note}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="from_arg", default=DEFAULT_UPSTREAM,
                    help="local private OMP checkout path or git URL "
                         "(default: https://github.com/Hunter-124/omp.git)")
    ap.add_argument("--sha", default=None, help="private OMP commit SHA (required for git URL)")
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
