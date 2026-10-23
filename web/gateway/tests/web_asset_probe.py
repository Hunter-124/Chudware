"""Live web static-asset delivery probe / ratchet.

perf_probe.py measures RPC latency through the /engine WS bridge; this measures
the OTHER half of web UX — first-load asset delivery. It hits the gateway's /app
static route (via FastAPI TestClient, no running server) against the REAL staged
bundle and records the on-the-wire transfer size per Accept-Encoding, proving the
brotli/gzip precompression (appserve._negotiate) stays live and effective.

Standalone:   CHUDWARE_DB_URL=<dev DB> CHUDWARE_WEB_ASSET_PROBE_EMAIL=<active Studio email> python web_asset_probe.py
Pytest:       pytest -m perf web_asset_probe.py  # isolated DB and probe account;
                                                    # skips when no staged bundle exists
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Dev auth (X-Dev-User) requires dev mode; set BEFORE any gateway.config import so
# the settings singleton reads dev=True (else the /app route 401s).
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data"))
# Assets to probe and the minimum brotli compression ratio we expect over the wire.
# wasm/js are highly compressible text-ish bytecode; .data is packed assets (partly
# already-compressed images) so it earns a gentler floor.
_ASSETS: dict[str, float] = {
    "Chudware.wasm": 3.0,
    "Chudware.js": 3.0,
    "Chudware.data": 1.5,
}


def _import_app():
    try:
        from gateway import app as app_mod
    except ImportError:
        import app as app_mod  # type: ignore
    return app_mod.app


def _staged_dir() -> Path | None:
    try:
        from gateway.config import settings
    except ImportError:
        from config import settings  # type: ignore
    root = getattr(settings, "app_dir", None)
    if not root:
        return None
    p = Path(root).expanduser()
    return p if (p / "Chudware.wasm").is_file() else None


def _transfer_size(client, name: str, accept: str) -> tuple[int, str | None]:
    """Return (bytes on the wire, content-encoding) for one asset request.

    We read Content-Length (the server sets it from the served file's size, i.e.
    the compressed variant when negotiated) rather than len(response.content),
    which httpx would have transparently decompressed."""
    r = client.get(
        f"/app/{name}",
        headers={"X-Dev-User": os.environ.get("CHUDWARE_WEB_ASSET_PROBE_EMAIL", "asset-probe@example.test"),
                 "Accept-Encoding": accept},
    )
    assert r.status_code == 200, f"{name} -> {r.status_code}"
    cl = r.headers.get("content-length")
    size = int(cl) if cl is not None else len(r.content)
    return size, r.headers.get("content-encoding")


def measure() -> dict[str, dict[str, Any]]:
    from fastapi.testclient import TestClient

    app = _import_app()
    out: dict[str, dict[str, Any]] = {}
    with TestClient(app) as client:
        for name in _ASSETS:
            raw, _ = _transfer_size(client, name, "identity")
            br, br_enc = _transfer_size(client, name, "br")
            gz, gz_enc = _transfer_size(client, name, "gzip")
            out[name] = {
                "raw": raw,
                "br": br,
                "br_enc": br_enc,
                "br_ratio": round(raw / max(1, br), 2),
                "gz": gz,
                "gz_enc": gz_enc,
                "gz_ratio": round(raw / max(1, gz), 2),
            }
    return out


def _fmt_mb(n: int) -> str:
    return f"{n / 1048576:.2f} MB"


def report() -> dict[str, dict[str, Any]]:
    m = measure()
    print(f"{'asset':<14}{'raw':>10}{'brotli':>10}{'ratio':>8}{'gzip':>10}{'ratio':>8}")
    tot_raw = tot_br = 0
    for name, s in m.items():
        tot_raw += s["raw"]
        tot_br += s["br"] if s["br_enc"] == "br" else s["raw"]
        print(
            f"{name:<14}{_fmt_mb(s['raw']):>10}{_fmt_mb(s['br']):>10}"
            f"{s['br_ratio']:>7}x{_fmt_mb(s['gz']):>10}{s['gz_ratio']:>7}x"
        )
    print(f"{'TOTAL first-load':<14}{_fmt_mb(tot_raw):>10}{_fmt_mb(tot_br):>10}"
          f"{round(tot_raw / max(1, tot_br), 2):>7}x")
    return m


@pytest.mark.perf
def test_web_asset_compression(activate, monkeypatch) -> None:
    """Assert the staged bundle is served compressed at the expected ratios."""
    if _staged_dir() is None:
        pytest.skip("no staged web bundle (Chudware.wasm) — run ./build.sh wasm first")
    from gateway.config import settings

    monkeypatch.setattr(settings, "dev_mode", True)
    activate(os.environ.get("CHUDWARE_WEB_ASSET_PROBE_EMAIL", "asset-probe@example.test"))
    m = report()
    for name, floor in _ASSETS.items():
        s = m[name]
        assert s["br_enc"] == "br", f"{name}: expected brotli, got {s['br_enc']}"
        assert s["br"] < s["raw"], f"{name}: brotli not smaller than raw"
        assert s["br_ratio"] >= floor, f"{name}: br ratio {s['br_ratio']} < {floor}"
        # gzip is the mandatory fallback for clients that don't accept brotli.
        assert s["gz_enc"] == "gzip", f"{name}: expected gzip fallback, got {s['gz_enc']}"


if __name__ == "__main__":
    if not os.environ.get("CHUDWARE_DB_URL") or not os.environ.get("CHUDWARE_WEB_ASSET_PROBE_EMAIL"):
        raise SystemExit("Set CHUDWARE_DB_URL to a dev database and CHUDWARE_WEB_ASSET_PROBE_EMAIL to an active Studio account")
    report()
