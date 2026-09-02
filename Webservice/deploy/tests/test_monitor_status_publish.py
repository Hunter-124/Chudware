"""Host-monitor exit-code tests; no R2 request and no nvidia-smi call is made."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1]
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import monitor as mon  # noqa: E402

R2_ENV = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT")


@pytest.fixture
def monitor(monkeypatch, tmp_path):
    """monitor.py rebound to a temp CHUDWARE_WEB_DATA with a hermetic sampler.

    DATA_ROOT/LOG_PATH/REFUSE_FLAG are module-level constants read at import, so
    the env var only takes effect through a reload; the reload is undone after
    the test so the real defaults are restored for anything else in the session.
    """
    monkeypatch.setenv("CHUDWARE_WEB_DATA", str(tmp_path))
    for name in R2_ENV:
        monkeypatch.delenv(name, raising=False)
    module = importlib.reload(mon)
    # Keep the host out of it: the publish branch is what is under test, not the
    # GPU/gateway probes (which would fork nvidia-smi and hit 127.0.0.1:8300).
    monkeypatch.setattr(module, "nvidia_sample", lambda: {"available": False})
    monkeypatch.setattr(module, "gateway_health", lambda: {"ok": True})
    try:
        yield module
    finally:
        monkeypatch.undo()
        importlib.reload(mon)


def _samples(module: Any) -> list[dict[str, Any]]:
    lines = module.LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_unconfigured_r2_logs_the_sample_and_keeps_the_unit_green(monitor, capsys):
    """chudware-monitor.service is a Type=oneshot on a two-minute timer: exiting
    non-zero because an optional publisher was never switched on pins the unit in
    `failed` forever, which is indistinguishable from the real host-monitor
    outage the timer exists to surface."""
    assert monitor.main([]) == 0

    samples = _samples(monitor)
    assert len(samples) == 1
    publish = samples[0]["status_publish"]
    assert publish["ok"] is False
    assert publish["skipped"] == "r2_not_configured"
    # The detail still has to name the credentials and the reason they are absent.
    for name in R2_ENV:
        assert name in publish["detail"]
    assert "10042" in publish["detail"]
    assert "chudware-monitor service environment" in publish["detail"]

    err = capsys.readouterr().err
    assert err.splitlines() == [
        f"monitor: status publish skipped (optional): {publish['detail']}"
    ]


def test_partially_configured_r2_is_a_real_fault(monkeypatch, monitor, capsys):
    """One key set means somebody tried to configure the publisher and got it
    wrong; that is a fault worth a red unit, unlike the never-enabled case."""
    monkeypatch.setenv("R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")

    assert monitor.main([]) == 2

    publish = _samples(monitor)[0]["status_publish"]
    assert publish["ok"] is False
    assert "skipped" not in publish
    assert "R2_ACCESS_KEY_ID" in publish["error"]
    assert "R2_ENDPOINT" not in publish["error"]
    assert "status publish failed" in capsys.readouterr().err


def test_configured_but_failing_upload_is_a_real_fault(monkeypatch, monitor, capsys):
    """A fully configured publisher whose upload raises must keep exiting 2."""
    for name in R2_ENV:
        monkeypatch.setenv(name, "set")
    monkeypatch.setattr(
        monitor,
        "publish_status",
        lambda record: (_ for _ in ()).throw(RuntimeError("boom: s3 error")),
    )

    assert monitor.main([]) == 2

    publish = _samples(monitor)[0]["status_publish"]
    assert publish == {"ok": False, "error": "boom: s3 error"}
    assert "boom: s3 error" in capsys.readouterr().err


def test_unconfigured_publish_raises_only_the_optional_exception(monitor):
    """Pin the split by type, not by message: widening the unconfigured branch to
    a bare RuntimeError would silently make the never-enabled case red again."""
    with pytest.raises(monitor.StatusPublishUnconfigured):
        monitor.publish_status({"ts": "1970-01-01T00:00:00Z"})
    assert issubclass(monitor.StatusPublishUnconfigured, RuntimeError)


def test_partial_publish_raises_plain_runtime_error(monkeypatch, monitor):
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "set")
    with pytest.raises(RuntimeError) as excinfo:
        monitor.publish_status({"ts": "1970-01-01T00:00:00Z"})
    assert not isinstance(excinfo.value, monitor.StatusPublishUnconfigured)


def test_dry_run_still_prints_the_sample_without_touching_r2(monitor, capsys):
    assert monitor.main(["--dry-run"]) == 0
    out = capsys.readouterr().out.strip()
    record = json.loads(out)
    assert record["dry_run"] is True
    assert record["status_publish"]["dry_run"] is True
    assert not monitor.LOG_PATH.exists()
