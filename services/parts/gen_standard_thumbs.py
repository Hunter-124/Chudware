#!/usr/bin/env python3
"""Pre-render indexed local CAD models into the shared preview cache.

Uses actual indexed IDs for native families, supported parametric recipes, and
locally available CAD files. Run with
``python -m services.parts.gen_standard_thumbs [--force] [--limit N]``.
"""
from __future__ import annotations

import argparse

from services import parts
from . import thumbs


def _preview_candidates() -> list[dict]:
    hub = parts._get_hub()
    hub._ensure_index()
    records = hub._index.search("", limit=hub._index.record_count)
    return [record for record in records
            if record.get("part_id") and thumbs._geometry_identity(record) is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-render even if already cached")
    parser.add_argument("--limit", type=int, default=0, help="stop after N renders (0 = all)")
    args = parser.parse_args(argv)

    records = _preview_candidates()
    if not records:
        print("no indexed part geometry found — nothing to render")
        return 1

    made = skipped = unavailable = failed = 0
    for record in records:
        part_id = str(record["part_id"])
        params = record.get("params") or {}
        image_url = str(params.get("image_url") or params.get("thumbnail") or "")
        identity = thumbs._geometry_identity(record)
        if identity is None:
            failed += 1
            print(f"  FAILED    {part_id}")
            continue
        image, miss = thumbs._cache_paths(part_id, image_url, "cad", identity)
        if args.force:
            image.unlink(missing_ok=True)
            miss.unlink(missing_ok=True)
        elif thumbs._cached_image(image):
            skipped += 1
            continue
        result = thumbs._warm_native_previews([{"item_id": part_id, "image_url": image_url}])[0]
        if result["ok"] and result["provenance"] == "cad_render":
            made += 1
            print(f"  rendered  {part_id}")
        elif result["reason"] == "no_preview":
            unavailable += 1
        else:
            failed += 1
            print(f"  FAILED    {part_id}: {result['reason']}")
        if args.limit and made >= args.limit:
            break

    print(f"\ndone: {made} rendered, {skipped} cached, {unavailable} without standalone CAD, "
          f"{failed} failed ({len(records)} indexed candidates) → {thumbs._cache_dir()}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
