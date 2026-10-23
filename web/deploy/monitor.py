#!/usr/bin/env python3
"""Host sampler, disk-pressure guard, public status publisher, and dashboards.

Run by the systemd user timer every two minutes, or:
  python3 monitor.py                       # sample, publish R2 status, log
  python3 monitor.py --dry-run             # print sample/status, no side effects
  python3 monitor.py --install-observability  # rootless Prometheus + Grafana
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import boto3
except ImportError:  # reported actionably only when an upload is attempted
    boto3 = None  # type: ignore[assignment]

# --- knobs -----------------------------------------------------------------
DATA_ROOT = Path(os.environ.get("CHUDWARE_WEB_DATA", Path.home() / "chudware-web-data"))
LOG_PATH = DATA_ROOT / "logs" / "monitor.jsonl"
REFUSE_FLAG = DATA_ROOT / "REFUSE_NEW"
GATEWAY_HEALTHZ = os.environ.get("CHUDWARE_GATEWAY_HEALTHZ", "http://127.0.0.1:8300/healthz")
DISK_REFUSE_PCT = float(os.environ.get("CHUDWARE_MONITOR_DISK_REFUSE_PCT", "10"))
LOG_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MiB
STATUS_BUCKET = os.environ.get("CHUDWARE_STATUS_BUCKET", "chudware-releases")
STATUS_KEY = os.environ.get("CHUDWARE_STATUS_KEY", "status.json").lstrip("/")
OBS_ROOT = DATA_ROOT / "observability"
GATEWAY_ENV = DATA_ROOT / "gateway.env"
PROMETHEUS_IMAGE = (
    "docker.io/prom/prometheus:v3.7.3@sha256:"
    "6573196e5dbe834779ac4d34cb053113cc502a69e9e20ba3be96d37f33e81545"
)
GRAFANA_IMAGE = (
    "docker.io/grafana/grafana:12.1.1@sha256:"
    "5749a0e982878aedaa2d320ed14d3bdce7a040a938f482258670009d419a597f"
)


def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001 — tolerate host noise
        return 1, "", str(exc)


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                parts = rest.strip().split()
                if not parts:
                    continue
                # values are kB
                try:
                    out[key] = int(parts[0]) * 1024
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def disk_sample(path: Path) -> dict[str, Any]:
    try:
        u = shutil.disk_usage(path if path.exists() else path.parent if path.parent.exists() else Path("/"))
        free_pct = (100.0 * u.free / u.total) if u.total else 0.0
        return {
            "path": str(path),
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "free_pct": round(free_pct, 2),
        }
    except OSError as exc:
        return {"path": str(path), "error": str(exc)}


def nvidia_sample() -> dict[str, Any]:
    """VRAM + encoder session count via nvidia-smi; tolerate missing driver/binary."""
    rc, out, err = _run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,utilization.gpu,utilization.encoder,encoder.stats.sessionCount",
            "--format=csv,noheader,nounits",
        ]
    )
    if rc != 0 or not out:
        return {"available": False, "error": err or out or f"rc={rc}"}
    gpus: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append(
                {
                    "mem_used_mib": float(parts[0]),
                    "mem_total_mib": float(parts[1]),
                    "util_gpu": float(parts[2]),
                    "util_encoder": float(parts[3]),
                    "encoder_sessions": int(float(parts[4])),
                }
            )
        except ValueError:
            gpus.append({"raw": line})
    return {"available": True, "gpus": gpus}


def gateway_health() -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(GATEWAY_HEALTHZ, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def set_refuse(want: bool, dry_run: bool) -> dict[str, Any] | None:
    exists = REFUSE_FLAG.exists()
    if want and not exists:
        if not dry_run:
            REFUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            REFUSE_FLAG.write_text(
                f"set by chudware-monitor at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                "disk free below threshold; the gateway is under disk pressure\n",
                encoding="utf-8",
            )
        return {"action": "refuse_new", "set": True, "dry_run": dry_run}
    if not want and exists:
        if not dry_run:
            try:
                REFUSE_FLAG.unlink()
            except OSError as exc:
                return {"action": "refuse_new", "set": False, "error": str(exc)}
        return {"action": "refuse_new", "set": False, "dry_run": dry_run}
    return None


def rotate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= LOG_ROTATE_BYTES:
            bak = path.with_suffix(path.suffix + ".1")
            if bak.exists():
                bak.unlink()
            path.rename(bak)
    except OSError:
        pass


def sample_and_act(*, dry_run: bool) -> dict[str, Any]:
    mi = meminfo()
    mem_avail = mi.get("MemAvailable", 0)
    mem_total = mi.get("MemTotal", 0)
    load1, load5, load15 = os.getloadavg()
    disk_data = disk_sample(DATA_ROOT)
    nvidia = nvidia_sample()
    health = gateway_health()

    actions: list[dict[str, Any]] = []

    # Disk refuse: free_pct on the data root below the threshold.
    free_pct = disk_data.get("free_pct")
    min_free = free_pct if isinstance(free_pct, (int, float)) else 100.0
    refuse_act = set_refuse(min_free < DISK_REFUSE_PCT, dry_run)
    if refuse_act:
        actions.append(refuse_act)

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "mem": {
            "available": mem_avail,
            "total": mem_total,
            "available_gib": round(mem_avail / (1024**3), 3) if mem_avail else 0,
        },
        "load": {"1": load1, "5": load5, "15": load15},
        "disk": {"data_root": disk_data},
        "nvidia": nvidia,
        "gateway_health": health is not None,
        "refuse_new": REFUSE_FLAG.exists() if not dry_run else (min_free < DISK_REFUSE_PCT or REFUSE_FLAG.exists()),
        "actions": actions,
        "dry_run": dry_run,
    }
    return record


def append_jsonl(record: dict[str, Any], dry_run: bool) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str)
    if dry_run:
        print(line)
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def public_status(record: dict[str, Any]) -> dict[str, Any]:
    """Public, non-sensitive snapshot consumed by the Pages status surface."""
    gateway_up = bool(record.get("gateway_health"))
    operational = gateway_up and not bool(record.get("refuse_new"))
    state = "operational" if operational else "degraded"
    service_state = "Operational" if gateway_up else "Service interruption"
    return {
        "summary": "Systems operational" if operational else "Service interruption",
        "state": state,
        "updated_at": record["ts"],
        "services": [
            {"name": "Account and billing", "state": service_state},
            {"name": "AI copilot gateway", "state": service_state},
            {
                "name": "Downloads",
                "state": "Operational" if gateway_up else "Status unavailable",
            },
        ],
    }


# Raised ONLY when the R2 status publisher was never switched on at all (none of
# the three variables set — the current state, since R2 itself is not enabled on
# the Cloudflare account). It must NOT fail the unit: chudware-monitor.service is
# a Type=oneshot driven by a two-minute timer, so one non-zero exit pins it in
# `failed` indefinitely, and a permanently red unit is indistinguishable from a
# real host-monitor outage — which is the actual thing the timer exists to
# surface (disk pressure, a dead gateway, leaked sessions). A half-configured
# publisher or a failed upload is a genuine fault and stays a loud RuntimeError.
class StatusPublishUnconfigured(RuntimeError):
    """The optional R2 status publisher is switched off, not broken."""


def publish_status(record: dict[str, Any]) -> dict[str, Any]:
    """Publish the Pages status JSON through R2's S3-compatible API."""
    required = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT")
    missing = [name for name in required if not (os.environ.get(name) or "").strip()]
    if missing:
        detail = (
            "status publish requires "
            + ", ".join(missing)
            + "; add them to the chudware-monitor service environment "
            "(R2 is not enabled until Cloudflare error 10042 is resolved)"
        )
        if len(missing) == len(required):
            raise StatusPublishUnconfigured(detail)
        raise RuntimeError(detail)
    if boto3 is None:
        raise RuntimeError(
            "status publish requires boto3; install it for /usr/bin/python3 "
            "or run monitor.py with Webservice/gateway/.venv/bin/python"
        )
    payload = json.dumps(public_status(record), separators=(",", ":")).encode("utf-8")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    client.put_object(
        Bucket=STATUS_BUCKET,
        Key=STATUS_KEY,
        Body=payload,
        ContentType="application/json",
        CacheControl="no-cache, max-age=0",
    )
    return {
        "ok": True,
        "bucket": STATUS_BUCKET,
        "key": STATUS_KEY,
        "bytes": len(payload),
    }


def _gateway_env_value(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        lines = GATEWAY_ENV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = name + "="
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def _write_private(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, mode)
    temp.replace(path)


def _podman(args: list[str], *, timeout: float = 180.0) -> str:
    rc, out, err = _run(["podman", *args], timeout=timeout)
    if rc != 0:
        detail = err or out or f"exit {rc}"
        raise RuntimeError(f"podman {' '.join(args[:3])} failed: {detail}")
    return out


def install_observability() -> dict[str, Any]:
    """Install/recreate the rootless Prometheus and Grafana containers."""
    metrics_token = _gateway_env_value("CHUDWARE_METRICS_ADMIN_TOKEN")
    grafana_password = _gateway_env_value("CHUDWARE_GRAFANA_ADMIN_PASSWORD")
    missing = [
        name
        for name, value in (
            ("CHUDWARE_METRICS_ADMIN_TOKEN", metrics_token),
            ("CHUDWARE_GRAFANA_ADMIN_PASSWORD", grafana_password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "observability install requires "
            + ", ".join(missing)
            + f" in the environment or {GATEWAY_ENV}"
        )
    rc, _, err = _run(["podman", "--version"])
    if rc != 0:
        raise RuntimeError(
            "rootless Podman is required for Prometheus/Grafana: "
            + (err or "podman executable not found")
        )

    prometheus_config = OBS_ROOT / "prometheus.yml"
    token_file = OBS_ROOT / "metrics-token"
    grafana_datasource = (
        OBS_ROOT / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
    )
    _write_private(
        prometheus_config,
        "global:\n"
        "  scrape_interval: 15s\n"
        "  evaluation_interval: 15s\n"
        "scrape_configs:\n"
        "  - job_name: chudware-gateway\n"
        "    metrics_path: /metrics\n"
        "    authorization:\n"
        "      type: Bearer\n"
        "      credentials_file: /etc/prometheus/metrics-token\n"
        "    static_configs:\n"
        "      - targets: ['127.0.0.1:8300']\n",
        mode=0o644,
    )
    _write_private(token_file, metrics_token + "\n")
    if any(char in grafana_password for char in "\r\n\x00"):
        raise RuntimeError("Grafana password must fit one environment-file value")
    _write_private(
        grafana_datasource,
        "apiVersion: 1\n"
        "datasources:\n"
        "  - name: Chudware Prometheus\n"
        "    type: prometheus\n"
        "    access: proxy\n"
        "    url: http://127.0.0.1:9090\n"
        "    isDefault: true\n"
        "    editable: false\n",
        mode=0o644,
    )

    _podman(["volume", "create", "--ignore", "chudware-prometheus-data"])
    _podman(["volume", "create", "--ignore", "chudware-grafana-data"])
    _podman(
        [
            "run",
            "-d",
            "--replace",
            "--name",
            "chudware-prometheus",
            "--network",
            "host",
            "--restart",
            "unless-stopped",
            "--user",
            "0:0",
            "-v",
            f"{prometheus_config}:/etc/prometheus/prometheus.yml:ro,Z",
            "-v",
            f"{token_file}:/etc/prometheus/metrics-token:ro,Z",
            "-v",
            "chudware-prometheus-data:/prometheus:Z",
            PROMETHEUS_IMAGE,
            "--config.file=/etc/prometheus/prometheus.yml",
            "--storage.tsdb.path=/prometheus",
            "--storage.tsdb.retention.time=30d",
            "--web.listen-address=127.0.0.1:9090",
        ]
    )
    # Podman consumes this file when creating the container; restarts use the
    # saved container configuration, not the host-side delivery file.
    with tempfile.TemporaryDirectory(prefix=".grafana-", dir=OBS_ROOT) as private:
        grafana_env = Path(private) / "grafana.env"
        _write_private(grafana_env, f"GF_SECURITY_ADMIN_PASSWORD={grafana_password}\n")
        _podman(
            [
                "run",
                "-d",
                "--replace",
                "--name",
                "chudware-grafana",
                "--network",
                "host",
                "--restart",
                "unless-stopped",
                "-e",
                "GF_SERVER_HTTP_ADDR=127.0.0.1",
                "-e",
                "GF_SECURITY_ADMIN_USER=admin",
                "--env-file",
                str(grafana_env),
                "-e",
                "GF_USERS_ALLOW_SIGN_UP=false",
                "-e",
                "GF_SERVER_HTTP_PORT=3000",
                "-v",
                f"{grafana_datasource.parent.parent}:/etc/grafana/provisioning:ro,Z",
                "-v",
                "chudware-grafana-data:/var/lib/grafana:Z",
                GRAFANA_IMAGE,
            ]
        )
    rc, out, err = _run(
        ["systemctl", "--user", "enable", "--now", "podman-restart.service"]
    )
    if rc != 0:
        raise RuntimeError(
            "containers started, but enabling podman-restart.service failed: "
            + (err or out or f"exit {rc}")
        )
    return {
        "ok": True,
        "prometheus": "http://127.0.0.1:9090",
        "grafana": "http://127.0.0.1:3000",
        "retention": "30d",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="chudware-web host monitor")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="print one JSONL sample and public status; no flag/log/R2 writes",
    )
    modes.add_argument(
        "--install-observability",
        action="store_true",
        help="install/recreate rootless Prometheus + Grafana containers",
    )
    args = ap.parse_args(argv)
    if args.install_observability:
        try:
            print(json.dumps(install_observability(), separators=(",", ":")))
        except RuntimeError as exc:
            print(f"monitor: {exc}", file=sys.stderr)
            return 2
        return 0

    record = sample_and_act(dry_run=args.dry_run)
    if args.dry_run:
        record["status_publish"] = {
            "dry_run": True,
            "bucket": STATUS_BUCKET,
            "key": STATUS_KEY,
            "body": public_status(record),
        }
        append_jsonl(record, dry_run=True)
        return 0

    publish_failed = False
    try:
        record["status_publish"] = publish_status(record)
    except StatusPublishUnconfigured as exc:
        # Deliberately off, so the sample still lands and the unit stays green;
        # one line on stderr keeps it visible in `journalctl -u chudware-monitor`.
        record["status_publish"] = {
            "ok": False,
            "skipped": "r2_not_configured",
            "detail": str(exc),
        }
        print(f"monitor: status publish skipped (optional): {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — preserve the local sampler record
        publish_failed = True
        record["status_publish"] = {"ok": False, "error": str(exc)}
        print(f"monitor: status publish failed: {exc}", file=sys.stderr)
    append_jsonl(record, dry_run=False)
    return 2 if publish_failed else 0


if __name__ == "__main__":
    sys.exit(main())
