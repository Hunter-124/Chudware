#!/usr/bin/env python3
"""Release-only entrypoint for the compiled worker and MCP sidecars."""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a compiled Chudware service sidecar.")
    parser.add_argument("--module", required=True, choices=("worker", "mcp"))
    args = parser.parse_args()

    if args.module == "worker":
        from services.worker_main import serve

        serve()
        return

    from services.mcp.server import main as serve_mcp

    serve_mcp()


if __name__ == "__main__":
    main()
