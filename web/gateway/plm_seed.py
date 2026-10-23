#!/usr/bin/env python3
"""Host-side PLM bare-remote seed for chudware-web (T-WEB2 §Q2).

Creates ``~/chudware-web-data/plm/<name>.git`` with a real ``refs/heads/master``
tip so session clones register ``origin`` and can sync later.

Usage (from Webservice/gateway)::

    python3 -m plm_seed Demo
    python3 plm_seed.py Demo --from /path/to/existing/project

Idempotent: an existing bare remote is a no-op with a message. Never pushes
from session seats — this tool only seeds the central bare on the host.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Repo root (…/CAD) so ``services.plm`` imports work with plain host python3.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dulwich.repo import Repo  # noqa: E402

from services.plm.remote import seed_remote  # noqa: E402
from services.plm.vault import Vault  # noqa: E402

_DEFAULT_SAMPLE = _REPO_ROOT / "samples" / "mech_part" / "bracket.aicm"
_CONTAINER_SUFFIXES = (".step+", ".ecad+")


def default_plm_dir() -> Path:
    return Path.home() / "chudware-web-data" / "plm"


def _looks_like_bare(path: Path) -> bool:
    return path.is_dir() and (
        (path / "objects").is_dir() or (path / "HEAD").is_file()
    )


def _copy_containers(src: Path, dest: Path) -> int:
    """Copy design containers (and rename .aicm → .step+) into *dest*."""
    n = 0
    for p in sorted(src.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        lower = name.lower()
        if lower.endswith(".aicm"):
            dest_name = name[: -len(".aicm")] + ".step+"
            if dest_name.lower() == "bracket.step+":
                dest_name = "Bracket.step+"
            shutil.copy2(p, dest / dest_name)
            n += 1
        elif any(lower.endswith(sfx) for sfx in _CONTAINER_SUFFIXES):
            shutil.copy2(p, dest / name)
            n += 1
    return n


def _ensure_tip(project_dir: Path, message: str = "Seed project with sample part") -> str:
    """Open vault and guarantee a real HEAD tip; return tip hex."""
    v = Vault.open(project_dir)
    try:
        head = v.status().get("head")
        if not head:
            res = v.save(message=message)
            head = res.get("commit") or v.status().get("head")
        if not head:
            res = v.checkpoint(message)
            head = res.get("commit") or v.status().get("head")
        if not head:
            raise SystemExit("plm_seed: failed to create a vault tip commit")
        return str(head)
    finally:
        v.close()


def prepare_template(project_dir: Path, *, from_dir: Path | None) -> str:
    """Populate *project_dir* (vault + tip) and return tip hex."""
    project_dir.mkdir(parents=True, exist_ok=True)

    if Vault.exists(project_dir):
        return _ensure_tip(project_dir)

    if from_dir is not None:
        src = from_dir.resolve()
        if not src.is_dir():
            raise SystemExit(f"plm_seed: --from is not a directory: {src}")
        if Vault.exists(src):
            # Caller handed us a vault path; use it in place (project_dir==src).
            if project_dir.resolve() != src:
                raise SystemExit(
                    "plm_seed: internal error: vault --from must be used as template root"
                )
            return _ensure_tip(project_dir)
        n = _copy_containers(src, project_dir)
        if n == 0:
            raise SystemExit(
                f"plm_seed: --from has no vault and no .step+/.ecad+/.aicm: {src}"
            )
    else:
        if not _DEFAULT_SAMPLE.is_file():
            raise SystemExit(f"plm_seed: default sample missing: {_DEFAULT_SAMPLE}")
        shutil.copy2(_DEFAULT_SAMPLE, project_dir / "Bracket.step+")

    return _ensure_tip(project_dir)


def _bare_tip_hex(bare: Path) -> str | None:
    rr = Repo(str(bare))
    try:
        try:
            tip = rr.refs[b"refs/heads/master"]
        except KeyError:
            return None
        if not tip or (isinstance(tip, bytes) and tip.startswith(b"ref: ")):
            return None
        return tip.decode("ascii") if isinstance(tip, bytes) else str(tip)
    finally:
        rr.close()


def seed(name: str, *, from_dir: Path | None = None, plm_dir: Path | None = None) -> int:
    name = (name or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise SystemExit(f"plm_seed: invalid project name: {name!r}")

    plm = (plm_dir or default_plm_dir()).expanduser().resolve()
    plm.mkdir(parents=True, exist_ok=True)
    bare = plm / f"{name}.git"

    if bare.exists() and (_looks_like_bare(bare) or any(bare.iterdir())):
        print(f"plm_seed: remote already exists (no-op): {bare}")
        return 0

    cleanup: Path | None = None
    if from_dir is not None and Vault.exists(from_dir):
        template = Path(from_dir).expanduser().resolve()
    else:
        cleanup = Path(tempfile.mkdtemp(prefix=f"plm-seed-{name}-"))
        template = cleanup

    try:
        prepare_template(template, from_dir=from_dir)
        out = seed_remote(str(template), str(bare))
        if not out.get("ok"):
            reason = out.get("reason") or "error"
            hint = out.get("hint") or ""
            raise SystemExit(f"plm_seed: seed_remote failed: {reason} {hint}".rstrip())

        tip_hex = _bare_tip_hex(bare)
        if not tip_hex:
            raise SystemExit(f"plm_seed: bare remote has no tip after seed: {bare}")

        print(f"plm_seed: seeded {bare} tip={tip_hex[:12]}")
        return 0
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="plm_seed",
        description="Seed a chudware-web PLM bare remote with a real tip commit.",
    )
    p.add_argument("name", help="Project name (becomes <name>.git under plm_dir)")
    p.add_argument(
        "--from",
        dest="from_dir",
        default=None,
        help="Existing project dir to seed from (vault or containers)",
    )
    p.add_argument(
        "--plm-dir",
        dest="plm_dir",
        default=None,
        help="Bare-remote root (default: ~/chudware-web-data/plm)",
    )
    args = p.parse_args(argv)
    from_dir = Path(args.from_dir).expanduser() if args.from_dir else None
    plm_dir = Path(args.plm_dir).expanduser() if args.plm_dir else None
    return seed(args.name, from_dir=from_dir, plm_dir=plm_dir)


if __name__ == "__main__":
    raise SystemExit(main())
