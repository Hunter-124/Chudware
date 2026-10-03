#!/usr/bin/env python3
"""Release-only entrypoint for the compiled worker and MCP sidecars."""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a compiled Chudware service sidecar.")
    parser.add_argument("--module", required=True, choices=(
        "worker", "mcp", "services.reveng.gaussian_jobs", "services.vision.gaussian_pipeline",
        "services.sim.job_worker",
    ))
    args, remaining = parser.parse_known_args()
    if args.module == "services.reveng.gaussian_jobs":
        from services.reveng.gaussian_jobs import main as supervise

        supervise(remaining)
        return
    if args.module == "services.vision.gaussian_pipeline":
        from services.vision.gaussian_pipeline import main as reconstruct

        raise SystemExit(reconstruct(remaining))
    if args.module == "services.sim.job_worker":
        from services.sim.job_worker import main as run_job

        # job_worker.main takes a full argv (program name, then the job id), exactly as
        # ``python -m services.sim.job_worker <job_id>`` hands it sys.argv.
        raise SystemExit(run_job(["services.sim.job_worker", *remaining]))
    if remaining:
        parser.error("unrecognized arguments: " + " ".join(remaining))

    if args.module == "worker":
        from services.worker_main import serve

        serve()
        return

    from services.mcp.server import main as serve_mcp

    serve_mcp()


if __name__ == "__main__":
    main()
