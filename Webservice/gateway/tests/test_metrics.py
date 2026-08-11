"""Metrics authorization, cardinality, and fail-open instrumentation contracts."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod, db, metrics  # noqa: E402
from gateway.config import settings  # noqa: E402

_LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"'
_SAMPLE = re.compile(
    rf"^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{{{_LABEL}(?:,{_LABEL})*\}})? "
    r"(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]Inf|NaN)$"
)


def _seed(email: str, *, admin: bool = False) -> None:
    accounts.ensure_account(email, display=email.split("@", 1)[0])
    accounts.set_status(email, "active", decided_by="test")
    if admin:
        with db.tx() as cur:
            cur.execute("UPDATE accounts SET role = 'admin' WHERE email = %s", (email,))


@pytest.fixture(autouse=True)
def _isolate(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", tmp_path / "data")
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(metrics, "registry", metrics.Registry())
    _seed("user@example.com")
    _seed("admin@example.com", admin=True)


@pytest.fixture
def client():
    with TestClient(app_mod.app) as value:
        yield value


def _auth(email: str) -> dict[str, str]:
    return {"X-Dev-User": email}


def test_metrics_requires_authentication(client):
    assert client.get("/metrics").status_code == 401


def test_metrics_rejects_authenticated_non_admin(client):
    assert client.get("/metrics", headers=_auth("user@example.com")).status_code == 401


def test_metrics_bearer_token_is_exact_and_sufficient(client, monkeypatch):
    monkeypatch.setenv("CHUDWARE_METRICS_ADMIN_TOKEN", "correct-operator-token")
    wrong = client.get(
        "/metrics", headers={"Authorization": "Bearer wrong-operator-token"}
    )
    assert wrong.status_code == 401
    valid = client.get(
        "/metrics", headers={"Authorization": "Bearer correct-operator-token"}
    )
    assert valid.status_code == 200
    assert "chudware_active_sessions" in valid.text


def test_admin_metrics_is_prometheus_text(client):
    response = client.get("/metrics", headers=_auth("admin@example.com"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    samples = [line for line in response.text.splitlines() if line and not line.startswith("#")]
    assert samples
    assert all(_SAMPLE.fullmatch(line) for line in samples), response.text
    assert "chudware_mail_outbox_depth" in response.text
    assert "chudware_webhook_requests_total" in response.text


def test_parameterized_routes_use_one_template_label(client):
    admin = _auth("admin@example.com")
    client.get("/api/admin/releases/download/first-build.exe", headers=admin)
    client.get("/api/admin/releases/download/second-build.exe", headers=admin)
    body = client.get("/metrics", headers=admin).text

    request_lines = [
        line for line in body.splitlines()
        if line.startswith("chudware_http_requests_total")
        and 'route="/api/admin/releases/download/{name}"' in line
    ]
    assert request_lines == [
        'chudware_http_requests_total{method="GET",route="/api/admin/releases/download/{name}",status="4xx"} 2'
    ]
    assert "first-build.exe" not in body
    assert "second-build.exe" not in body


def test_instrumentation_exception_cannot_break_response(client, monkeypatch):
    def broken_observe(*args, **kwargs):
        raise RuntimeError("synthetic metrics failure")

    monkeypatch.setattr(metrics.registry, "observe", broken_observe)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
