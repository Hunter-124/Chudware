#!/usr/bin/env python3
"""billet-web host monitor — sample host health, reaper actions, JSONL log.

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
DATA_ROOT = Path(os.environ.get("BILLET_WEB_DATA", Path.home() / "billet-web-data"))
LOG_PATH = DATA_ROOT / "logs" / "monitor.jsonl"
REFUSE_FLAG = DATA_ROOT / "REFUSE_NEW"
GATEWAY_HEALTHZ = os.environ.get("BILLET_GATEWAY_HEALTHZ", "http://127.0.0.1:8300/healthz")
CONTAINER_STORAGE = Path(
    os.environ.get(
        "BILLET_CONTAINER_STORAGE",
        Path.home() / ".local/share/containers/storage",
    )
)
MEM_ACT_BYTES = int(os.environ.get("BILLET_MONITOR_MEM_ACT", str(4 * 1024**3)))  # 4 GiB
DISK_REFUSE_PCT = float(os.environ.get("BILLET_MONITOR_DISK_REFUSE_PCT", "10"))
LOG_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MiB
SESSION_NAME_PREFIX = "billet-sess-"


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


def podman_sessions() -> list[dict[str, Any]]:
    rc, out, err = _run(
        [
            "podman",
            "ps",
            "--filter",
            f"name={SESSION_NAME_PREFIX}",
            "--format",
            "{{.Names}}\t{{.Status}}\t{{.CreatedAt}}\t{{.ID}}",
        ]
    )
    if rc != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append(
            {
                "name": parts[0].strip(),
                "status": parts[1].strip(),
                "created": parts[2].strip(),
                "id": parts[3].strip() if len(parts) > 3 else "",
            }
        )
    return rows


def gateway_health() -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(GATEWAY_HEALTHZ, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def pick_oldest_idle(sessions: list[dict[str, Any]], health: dict[str, Any] | None) -> str | None:
    """Prefer gateway-reported idle sessions; fall back to oldest running container."""
    if not sessions:
        return None

    idle_names: list[str] = []
    if isinstance(health, dict):
        # Prefer explicit idle list; else sessions[] entries marked idle=true.
        for key in ("idle_sessions", "sessions"):
            val = health.get(key)
            if not isinstance(val, list):
                continue
            for item in val:
                if isinstance(item, str):
                    if key == "idle_sessions":
                        idle_names.append(item)
                elif isinstance(item, dict):
                    name = item.get("container") or item.get("name") or item.get("id")
                    if not isinstance(name, str) or not name:
                        continue
                    if key == "idle_sessions" or item.get("idle") is True:
                        idle_names.append(
                            name
                            if name.startswith(SESSION_NAME_PREFIX)
                            else f"{SESSION_NAME_PREFIX}{name}"
                        )
            if key == "idle_sessions" and idle_names:
                break

    by_name = {s["name"]: s for s in sessions}
    for n in idle_names:
        if n in by_name:
            return n
        # slug-only match
        for sname in by_name:
            if sname == n or sname.endswith(n) or n.endswith(sname):
                return sname

    # Fall back: oldest by CreatedAt string order is unreliable; use podman inspect if few.
    # Prefer reverse of list order (podman ps default is newest-first) → last is oldest.
    return sessions[-1]["name"] if sessions else None


def stop_session(name: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"action": "stop", "target": name, "dry_run": True, "ok": True}
    rc, out, err = _run(["podman", "stop", "-t", "20", name], timeout=60.0)
    return {
        "action": "stop",
        "target": name,
        "ok": rc == 0,
        "rc": rc,
        "stdout": out[:500],
        "stderr": err[:500],
    }


def set_refuse(want: bool, dry_run: bool) -> dict[str, Any] | None:
    exists = REFUSE_FLAG.exists()
    if want and not exists:
        if not dry_run:
            REFUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            REFUSE_FLAG.write_text(
                f"set by billet-monitor at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                "disk free below threshold; gateway must refuse new sessions\n",
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
    disk_ctr = disk_sample(CONTAINER_STORAGE)
    nvidia = nvidia_sample()
    sessions = podman_sessions()
    health = gateway_health()

    actions: list[dict[str, Any]] = []

    # Disk refuse: free_pct on data root OR container storage < threshold
    free_pcts = [
        d.get("free_pct")
        for d in (disk_data, disk_ctr)
        if isinstance(d.get("free_pct"), (int, float))
    ]
    min_free = min(free_pcts) if free_pcts else 100.0
    refuse_act = set_refuse(min_free < DISK_REFUSE_PCT, dry_run)
    if refuse_act:
        actions.append(refuse_act)

    # Low RAM: stop oldest idle session
    if mem_avail and mem_avail < MEM_ACT_BYTES and sessions:
        target = pick_oldest_idle(sessions, health)
        if target:
            actions.append(stop_session(target, dry_run))
            actions.append(
                {
                    "action": "reason",
                    "why": "MemAvailable_below_threshold",
                    "mem_available": mem_avail,
                    "threshold": MEM_ACT_BYTES,
                }
            )

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "mem": {
            "available": mem_avail,
            "total": mem_total,
            "available_gib": round(mem_avail / (1024**3), 3) if mem_avail else 0,
        },
        "load": {"1": load1, "5": load5, "15": load15},
        "disk": {"data_root": disk_data, "container_storage": disk_ctr},
        "nvidia": nvidia,
        "sessions": sessions,
        "session_count": len(sessions),
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
    ap = argparse.ArgumentParser(description="billet-web host monitor")
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
