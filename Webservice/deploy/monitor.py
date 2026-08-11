#!/usr/bin/env python3
"""chudware-web host monitor — sample host health, disk-pressure flag, JSONL log.

stdlib only. Run by systemd user timer every 2 min, or:
  python3 monitor.py            # sample + act + append JSONL
  python3 monitor.py --dry-run  # print one JSONL sample, no side effects
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --- knobs -----------------------------------------------------------------
DATA_ROOT = Path(os.environ.get("CHUDWARE_WEB_DATA", Path.home() / "chudware-web-data"))
LOG_PATH = DATA_ROOT / "logs" / "monitor.jsonl"
REFUSE_FLAG = DATA_ROOT / "REFUSE_NEW"
GATEWAY_HEALTHZ = os.environ.get("CHUDWARE_GATEWAY_HEALTHZ", "http://127.0.0.1:8300/healthz")
DISK_REFUSE_PCT = float(os.environ.get("CHUDWARE_MONITOR_DISK_REFUSE_PCT", "10"))
LOG_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MiB


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="chudware-web host monitor")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print one JSONL sample to stdout; no stop/flag/log writes",
    )
    args = ap.parse_args(argv)
    record = sample_and_act(dry_run=args.dry_run)
    append_jsonl(record, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
