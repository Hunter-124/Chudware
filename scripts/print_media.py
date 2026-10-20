#!/usr/bin/env python3
"""Inspect or publish printer media through the same managed workflow as CAD agents.

Inspect first: python scripts/print_media.py inventory /mounted/drive
Publish only idle media, using that inventory_token and a JSON list of complete jobs:
  python scripts/print_media.py publish /mounted/drive --project ROBARM \
      --jobs-file jobs.json --expected-inventory TOKEN --media-idle
Each job is {"part_id", "manifest_path", "gcode_path"?, "user_override"?}; a job that is
not print_ready publishes only with "user_override" naming why it goes on anyway.
Explicit --retire-path values delete reviewed legacy files; unknown files stay untouched.
Raw print files go to the drive root by default; --layout project uses one top-level
project folder. Neither layout creates nested delivery directories.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.manufacturing.print_media import inventory_print_media, publish_print_media


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="Read the actual media tree and revision state")
    inventory.add_argument("root")
    inventory.add_argument("--offset", type=int, default=0)
    inventory.add_argument("--limit", type=int, default=200)
    publish = commands.add_parser("publish", help="Publish the complete verified project print set")
    publish.add_argument("root")
    publish.add_argument("--project", required=True)
    publish.add_argument("--jobs-file", type=Path, required=True)
    publish.add_argument("--expected-inventory", required=True)
    publish.add_argument("--layout", choices=("root", "project"), default="root",
                         help="Raw files at drive root (default), or one top-level project folder")
    publish.add_argument("--media-idle", action="store_true",
                         help="Confirm media is not being used by any printer; never infer this")
    publish.add_argument("--retire-path", action="append", default=[],
                         help="Reviewed relative legacy printable path to delete; repeat as needed")
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            result = inventory_print_media(args.root, offset=args.offset, limit=args.limit)
        else:
            jobs = json.loads(args.jobs_file.read_text(encoding="utf-8"))
            result = publish_print_media(args.root, args.project, jobs,
                                         args.expected_inventory, media_idle=args.media_idle,
                                         retire_paths=args.retire_path, layout=args.layout)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "reason": str(exc)}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
