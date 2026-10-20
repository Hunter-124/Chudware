"""Live tenant-cell + gateway WS bridge performance probe / ratchet.

Run standalone (cwd = Webservice/gateway)::

    CHUDWARE_LIVE=1 .venv/bin/python tests/perf_probe.py [--rebaseline]

Or via pytest (skipped unless CHUDWARE_LIVE=1)::

    CHUDWARE_LIVE=1 .venv/bin/pytest -m perf tests/perf_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

GATEWAY_DIR = Path(__file__).resolve().parents[1]
WEBSERVICE_DIR = GATEWAY_DIR.parent
PERF_DIR = WEBSERVICE_DIR / "perf"
BASELINE_PATH = PERF_DIR / "baseline.json"
TRACES_DIR = PERF_DIR / "traces"

PERF_ACCOUNT = os.environ.get("CHUDWARE_PERF_ACCOUNT", "")
DEV_HEADERS = {"X-Dev-User": PERF_ACCOUNT}
N_ITERS = 20
P95_REGRESS_FRAC = 0.15  # fail if p95 grows >15% vs baseline...
P95_REGRESS_FLOOR_MS = 0.1  # ...AND by >0.1 ms absolute (sub-ms p95 noise floor)
BRIDGE_OVERHEAD_GROW_MS = 2.0  # fail if bridge overhead p50 grows >2 ms abs

# Exact add_feature box payload (contracts/doc_ops.md + cad_tools.py example).
BOX_OP: dict[str, Any] = {
    "op": "add_feature",
    "feature": {
        "type": "box",
        "name": "Block",
        "params": {
            "length": {"value": 40, "expr": None},
            "width": {"value": 30, "expr": None},
            "height": {"value": 20, "expr": None},
        },
    },
}

# Methods whose p50/p95 we always report (measure_part optional).
CORE_METHODS = ("hello", "doc_new", "doc_apply_ops", "doc_undo")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(samples: list[float], p: float) -> float:
    """Linear-interpolated percentile; p in [0, 100]."""
    if not samples:
        return 0.0
    s = sorted(samples)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _stats(samples: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(_percentile(samples, 50), 4),
        "p95_ms": round(_percentile(samples, 95), 4),
        "n": len(samples),
        "mean_ms": round(statistics.fmean(samples), 4) if samples else 0.0,
    }


def _git_short_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(WEBSERVICE_DIR.parent),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _require_live() -> None:
    if os.environ.get("CHUDWARE_LIVE") != "1":
        raise RuntimeError(
            "CHUDWARE_LIVE=1 is required to run the live tenant-cell perf probe"
        )
    if not PERF_ACCOUNT:
        raise RuntimeError("CHUDWARE_PERF_ACCOUNT must name an existing approved test account")
    from gateway import cells
    principal = cells.resolve_workspace(PERF_ACCOUNT)
    cells.prepare_cell(principal)
    asyncio.run(cells.start_cell(principal.cell_id))


# ---------------------------------------------------------------------------
# Direct unix-socket JSON-RPC client
# ---------------------------------------------------------------------------


class UnixRpc:
    """Newline-delimited JSON-RPC over a unix domain socket."""

    def __init__(self) -> None:
        from gateway import cells
        self.principal = cells.resolve_workspace(PERF_ACCOUNT)
        self._sock: socket.socket | None = None
        self._r: Any = None
        self._w: Any = None
        self._next_id = 0

    def connect(self) -> None:
        self.close()
        from gateway import cells
        from gateway.config import settings
        s = cells._connect_pinned(settings.cells_root / self.principal.cell_id)
        s.settimeout(60.0)
        self._sock = s
        # Buffered reader (default) — unbuffered makefile readline is 1-byte/syscall
        # and inflates timings on large hostd responses. Writer is unbuffered + flush.
        self._r = s.makefile("rb")
        self._w = s.makefile("wb", buffering=0)
        self._next_id = 0

    def close(self) -> None:
        for f in (self._r, self._w):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        self._r = None
        self._w = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._r is None or self._w is None:
            raise RuntimeError("UnixRpc not connected")
        self._next_id += 1
        mid = self._next_id
        if method == "hello":
            params = dict(params or {}, principal=self.principal.hello())
        line = json.dumps(
            {"id": mid, "method": method, "params": params or {}},
            separators=(",", ":"),
        )
        self._w.write((line + "\n").encode("utf-8"))
        # buffering=0 write still needs an explicit flush on some platforms
        try:
            self._w.flush()
        except (AttributeError, OSError):
            pass
        while True:
            raw = self._r.readline()
            if not raw:
                raise ConnectionError(f"EOF waiting for {method} id={mid}")
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg
            # id-less event push lines — ignore

    def __enter__(self) -> "UnixRpc":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Direct battery
# ---------------------------------------------------------------------------


def _hello_params() -> dict[str, Any]:
    return {"client": "gui", "pid": os.getpid(), "proto": 1}


def measure_direct() -> tuple[dict[str, dict[str, float]], str]:
    """Run the direct hostd battery. Returns (metrics, note)."""
    note_parts: list[str] = []
    samples: dict[str, list[float]] = {m: [] for m in CORE_METHODS}

    # 1) hello — reconnect per iteration
    for _ in range(N_ITERS):
        rpc = UnixRpc()
        t0 = time.perf_counter()
        rpc.connect()
        resp = rpc.call("hello", _hello_params())
        dt = (time.perf_counter() - t0) * 1000.0
        rpc.close()
        if "error" in resp:
            raise RuntimeError(f"hello failed: {resp}")
        samples["hello"].append(dt)

    # 2) doc_new — 20 create+close cycles (standalone samples)
    for i in range(N_ITERS):
        with UnixRpc() as rpc:
            rpc.call("hello", _hello_params())
            t0 = time.perf_counter()
            resp = rpc.call("doc_new", {"name": f"perf_direct_{i}"})
            dt = (time.perf_counter() - t0) * 1000.0
            if "error" in resp:
                raise RuntimeError(f"doc_new failed: {resp}")
            samples["doc_new"].append(dt)
            doc_id = (resp.get("result") or {}).get("doc_id")
            if doc_id:
                rpc.call("doc_close", {"doc_id": doc_id})

    # 3–5) one doc for the op battery: apply_ops / measure_part? / undo
    measure_ok = False
    with UnixRpc() as rpc:
        rpc.call("hello", _hello_params())
        resp = rpc.call("doc_new", {"name": "perf_direct_ops"})
        if "error" in resp:
            raise RuntimeError(f"doc_new (ops) failed: {resp}")
        doc_id = (resp.get("result") or {}).get("doc_id")
        if not doc_id:
            raise RuntimeError(f"doc_new missing doc_id: {resp}")

        # Probe measure_part once
        probe = rpc.call("measure_part", {"doc_id": doc_id})
        if "error" not in probe:
            measure_ok = True
            samples["measure_part"] = []
        else:
            err = probe.get("error") or {}
            note_parts.append(
                "measure_part unavailable on hostd "
                f"({err.get('message') or err.get('error') or 'error'}); "
                "doc_apply_ops + doc_undo only"
            )

        for _ in range(N_ITERS):
            t0 = time.perf_counter()
            resp = rpc.call(
                "doc_apply_ops",
                {"doc_id": doc_id, "ops": [BOX_OP]},
            )
            dt = (time.perf_counter() - t0) * 1000.0
            if "error" in resp:
                raise RuntimeError(f"doc_apply_ops failed: {resp}")
            samples["doc_apply_ops"].append(dt)

            if measure_ok:
                t0 = time.perf_counter()
                resp = rpc.call("measure_part", {"doc_id": doc_id})
                dt = (time.perf_counter() - t0) * 1000.0
                if "error" in resp:
                    raise RuntimeError(f"measure_part failed: {resp}")
                samples["measure_part"].append(dt)

            t0 = time.perf_counter()
            resp = rpc.call("doc_undo", {"doc_id": doc_id})
            dt = (time.perf_counter() - t0) * 1000.0
            if "error" in resp:
                raise RuntimeError(f"doc_undo failed: {resp}")
            samples["doc_undo"].append(dt)

        rpc.call("doc_close", {"doc_id": doc_id})

    metrics = {name: _stats(vals) for name, vals in samples.items()}
    return metrics, "; ".join(note_parts)


# ---------------------------------------------------------------------------
# Gateway WS bridge battery (FastAPI TestClient)
# ---------------------------------------------------------------------------


def _import_app():
    """Import gateway app with CHUDWARE_WEB_DEV=1 so X-Dev-User auth works."""
    os.environ["CHUDWARE_WEB_DEV"] = "1"
    os.environ.setdefault(
        "CHUDWARE_WEB_DATA_ROOT",
        str(Path.home() / "chudware-web-data-perf"),
    )
    # Prefer package import used by other tests; fall back to `from app import app`.
    parent = str(GATEWAY_DIR.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    gw = str(GATEWAY_DIR)
    if gw not in sys.path:
        sys.path.insert(0, gw)

    # Drop cached modules so settings re-read env if needed.
    for mod in (
        "gateway.config",
        "gateway.app",
        "gateway.engine_bridge",
        "gateway.auth",
        "config",
        "app",
        "engine_bridge",
        "auth",
    ):
        sys.modules.pop(mod, None)

    try:
        from gateway import app as app_mod  # type: ignore

        return app_mod.app
    except ImportError:
        from app import app as fastapi_app  # type: ignore

        return fastapi_app


def _ws_rpc(ws, mid: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    ws.send_text(
        json.dumps(
            {"id": mid, "method": method, "params": params or {}},
            separators=(",", ":"),
        )
    )
    while True:
        msg = json.loads(ws.receive_text())
        if msg.get("id") == mid:
            return msg


def measure_bridge() -> dict[str, dict[str, float]]:
    """Measure the contained browser contract; CAD remains direct-socket only."""
    from fastapi.testclient import TestClient

    app = _import_app()
    samples: dict[str, list[float]] = {"hello": [], "ping": []}
    next_id = 0

    def nid() -> int:
        nonlocal next_id
        next_id += 1
        return next_id

    client = TestClient(app)

    # 1) hello — reconnect per iteration (new WS each time)
    for _ in range(N_ITERS):
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            mid = nid()
            t0 = time.perf_counter()
            resp = _ws_rpc(ws, mid, "hello", _hello_params())
            dt = (time.perf_counter() - t0) * 1000.0
            if "error" in resp:
                raise RuntimeError(f"bridge hello failed: {resp}")
            samples["hello"].append(dt)

    with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
        for _ in range(N_ITERS):
            mid = nid()
            t0 = time.perf_counter()
            resp = _ws_rpc(ws, mid, "ping")
            dt = (time.perf_counter() - t0) * 1000.0
            if resp.get("result") != {"ok": True}:
                raise RuntimeError(f"bridge ping failed: {resp}")
            samples["ping"].append(dt)

    return {name: _stats(vals) for name, vals in samples.items()}


# ---------------------------------------------------------------------------
# Ratchet + I/O
# ---------------------------------------------------------------------------


def _bridge_overhead(
    direct: dict[str, dict[str, float]],
    bridge: dict[str, dict[str, float]],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for method in sorted(set(direct) | set(bridge)):
        if method not in direct or method not in bridge:
            continue
        out[method] = round(bridge[method]["p50_ms"] - direct[method]["p50_ms"], 4)
    return out


def _print_table(
    direct: dict[str, dict[str, float]],
    bridge: dict[str, dict[str, float]],
    overhead: dict[str, float],
    baseline: dict[str, Any] | None,
) -> list[str]:
    """Print comparison table; return list of failure messages."""
    methods = sorted(set(direct) | set(bridge))
    headers = (
        f"{'method':16} {'d_p50':>8} {'d_p95':>8} {'b_p50':>8} {'b_p95':>8} "
        f"{'oh_p50':>8} {'base_p95':>9} {'base_oh':>8} {'status':>8}"
    )
    print(headers)
    print("-" * len(headers))
    failures: list[str] = []
    base_metrics = (baseline or {}).get("metrics") or {}
    base_oh = (baseline or {}).get("bridge_overhead_ms") or {}

    for m in methods:
        d = direct.get(m) or {}
        b = bridge.get(m) or {}
        oh = overhead.get(m)
        bm = base_metrics.get(m) or {}
        boh = base_oh.get(m)
        status = "ok"
        if baseline and m in base_metrics:
            base_p95 = float(bm.get("p95_ms", 0))
            cur_p95 = float(d.get("p95_ms", 0))
            limit = max(base_p95 * (1.0 + P95_REGRESS_FRAC), base_p95 + P95_REGRESS_FLOOR_MS)
            if base_p95 > 0 and cur_p95 > limit:
                status = "REGRESS"
                failures.append(
                    f"{m}: direct p95 {cur_p95:.3f} ms > "
                    f"{limit:.3f} ms "
                    f"(baseline {base_p95:.3f} +15% or +{P95_REGRESS_FLOOR_MS} ms)"
                )
            if boh is not None and oh is not None:
                if oh > float(boh) + BRIDGE_OVERHEAD_GROW_MS:
                    status = "REGRESS"
                    failures.append(
                        f"{m}: bridge_overhead p50 {oh:.3f} ms > "
                        f"baseline {float(boh):.3f} + {BRIDGE_OVERHEAD_GROW_MS} ms"
                    )
        print(
            f"{m:16} {d.get('p50_ms', float('nan')):8.3f} "
            f"{d.get('p95_ms', float('nan')):8.3f} "
            f"{b.get('p50_ms', float('nan')):8.3f} "
            f"{b.get('p95_ms', float('nan')):8.3f} "
            f"{(oh if oh is not None else float('nan')):8.3f} "
            f"{float(bm.get('p95_ms', float('nan'))):9.3f} "
            f"{float(boh if boh is not None else float('nan')):8.3f} "
            f"{status:>8}"
        )
    return failures


def run_probe(*, rebaseline: bool = False) -> dict[str, Any]:
    _require_live()
    PERF_DIR.mkdir(parents=True, exist_ok=True)
    TRACES_DIR.mkdir(parents=True, exist_ok=True)

    from gateway import cells
    principal = cells.resolve_workspace(PERF_ACCOUNT)
    print(f"tenant cell: {principal.cell_id}")
    print(f"iterations:   {N_ITERS}")
    print("measuring DIRECT …")
    direct, note = measure_direct()
    print("measuring BRIDGE …")
    bridge = measure_bridge()
    overhead = _bridge_overhead(direct, bridge)

    sha = _git_short_sha()
    ts = _utc_iso()
    payload: dict[str, Any] = {
        "sha": sha,
        "ts": ts,
        "metrics": direct,
        "bridge": bridge,
        "bridge_overhead_ms": overhead,
        "note": note,
        "n_iters": N_ITERS,
        "cell_id": principal.cell_id,
    }

    # Always write a trace.
    trace_name = f"{ts}-{sha}.json"
    trace_path = TRACES_DIR / trace_name
    trace_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"trace: {trace_path}")

    baseline: dict[str, Any] | None = None
    if BASELINE_PATH.exists() and not rebaseline:
        baseline = json.loads(BASELINE_PATH.read_text())

    failures = _print_table(direct, bridge, overhead, baseline)

    if baseline is None or rebaseline:
        BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"baseline written: {BASELINE_PATH}")
    elif failures:
        print("\nRATCHET FAILURES:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    else:
        print("ratchet: PASS")

    return payload


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chudware hostd/gateway perf probe")
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help="overwrite Webservice/perf/baseline.json with current metrics",
    )
    args = parser.parse_args(argv)
    try:
        run_probe(rebaseline=args.rebaseline)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"perf_probe ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


@pytest.mark.perf
@pytest.mark.skipif(
    os.environ.get("CHUDWARE_LIVE") != "1",
    reason="live hostd probe requires CHUDWARE_LIVE=1",
)
def test_perf_probe() -> None:
    """Pytest entry: same battery + ratchet as the standalone script."""
    run_probe(rebaseline=False)


if __name__ == "__main__":
    sys.exit(main())
