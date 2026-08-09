"""Clean-process entry point for the native engine agreement probe."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

from .agreement import _compare_with_engine_in_process


def main() -> int:
    payload = json.load(sys.stdin)
    result = _compare_with_engine_in_process(
        part_index=int(payload["part_index"]),
        seed=int(payload["seed"]),
        resolution=(int(payload["resolution"][0]), int(payload["resolution"][1])),
        library=Path(payload["library"]),
        output_dir=Path(payload["output_dir"]),
    )
    value = asdict(result)
    value["engine_path"] = str(result.engine_path)
    print(json.dumps(value, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
