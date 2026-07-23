#!/usr/bin/env python3
"""engine/worker.py — transition shim (T-RF20/R5a).

All services-side worker methods now register in `services/worker_main.py` (the
dual-path worker that survives the engine_oracle/ deletion). This shim exists so
existing spawn targets (`python -m engine.worker` from host bootstrap paths, test
harnesses, and the hostd worker bridge until it flips) keep working while the
callers migrate. Deleted at R5a together with the `engine` symlink.
"""
from services.worker_main import serve

if __name__ == "__main__":
    serve()
