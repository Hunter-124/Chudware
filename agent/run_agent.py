"""CLI: drive the full agent loop from a natural-language request.

    python -m agent.run_agent "a 50mm cube with a 10mm hole through the center and rounded edges"

Uses the configured provider (default Anthropic; needs ANTHROPIC_API_KEY in the env or
%APPDATA%/AI-CAD/config.json). Outputs land in %APPDATA%/AI-CAD/cache/<name>.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from agent import config, loop


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    request = " ".join(argv)
    try:
        provider = config.get_provider()
    except Exception as e:  # noqa: BLE001
        print(f"Provider error: {e}\nSet ANTHROPIC_API_KEY or %APPDATA%/AI-CAD/config.json.",
              file=sys.stderr)
        return 2

    print(f"[provider: {provider.name}] designing: {request}\n", file=sys.stderr)
    run = loop.run(request, provider)

    summary = {
        "success": run.success,
        "iterations": run.iterations,
        "contact_sheet": run.contact_sheet,
        "out_dir": run.out_dir,
        "bbox": (run.result or {}).get("bbox"),
        "volume_mm3": (run.result or {}).get("volume"),
        "step_path": (run.result or {}).get("step_path"),
        "log": run.log,
    }
    print(json.dumps(summary, indent=2))
    if run.final_code:
        (Path(run.out_dir) / "agent_model.py").write_text(run.final_code, encoding="utf-8")
    return 0 if run.success else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
