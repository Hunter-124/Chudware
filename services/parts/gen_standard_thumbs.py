#!/usr/bin/env python3
"""Pre-render 3D CAD-model thumbnails for every standard (mech_family) library part.

Template parts + scripts generate the sizes/variations: each parametric
family+size is instantiated via the core ``parts_make`` (the template part) and rendered
to a PNG, warming the same on-disk thumbnail cache the GUI's ``parts_thumbnail`` RPC reads
(keyed by ``sha1(part_id)``). With the cache warm, the Parts Library shows the real model
screenshot instantly instead of the 2D domain glyph; ``parts_thumbnail`` still renders on
demand for any part not covered here.

Usage:
    python -m services.parts.gen_standard_thumbs [--force] [--limit N]

Idempotent: skips part_ids already cached unless ``--force`` is given.
"""
from __future__ import annotations

import argparse
import hashlib

# The core, as the PACKAGE module — the same object gui_rpc._render_local_part_png uses. Never
# a bare `import core_bridge` off a sys.path hack: that leaves __package__ empty and
# core_bridge.call()'s own relative import of daemon_bridge then fails in daemon mode.
import services.mcp.core_bridge as _core_bridge  # noqa: E402

# Reuse the exact cache dir + render helper the RPC uses, so keys/paths can never drift.
from .gui_rpc import (  # noqa: E402
    _render_local_part_png,
    _thumb_cache_dir,
)


def _catalog() -> list[dict]:
    result = _core_bridge.call("parts_catalog_list", {})
    fams = result if isinstance(result, list) else (result or {}).get("families", [])
    return fams if isinstance(fams, list) else []


def _part_ids() -> list[str]:
    ids: list[str] = []
    for fam in _catalog():
        if not isinstance(fam, dict):
            continue
        family = fam.get("key") or fam.get("family") or ""
        if not family:
            continue
        for size in fam.get("sizes") or []:
            ids.append(f"mech_family:{family}:{size}")
    return ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-render even if already cached")
    ap.add_argument("--limit", type=int, default=0, help="stop after N renders (0 = all)")
    args = ap.parse_args(argv)

    cache_dir = _thumb_cache_dir()
    ids = _part_ids()
    if not ids:
        print("no standard parts found (native core unavailable?) — nothing to render")
        return 1

    made = skipped = failed = 0
    for pid in ids:
        cache = cache_dir / (hashlib.sha1(pid.encode()).hexdigest() + ".img")
        if cache.exists() and not args.force:
            skipped += 1
            continue
        data = _render_local_part_png(pid, str(cache) + ".png")
        if data:
            cache.write_bytes(data)
            made += 1
            print(f"  rendered  {pid}")
        else:
            failed += 1
            print(f"  FAILED    {pid}")
        if args.limit and made >= args.limit:
            break

    print(f"\ndone: {made} rendered, {skipped} cached, {failed} failed "
          f"({len(ids)} standard parts) → {cache_dir}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
