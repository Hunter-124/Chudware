"""Benchmark category registry for the Settings → Benchmarks tab.

Maps category id → runner invocation + last-result reader. Consumed by the
``bench_categories`` / ``bench_run`` (with ``category``) RPCs in
``services/agent/worker_rpc.py`` and listed for the GUI coverage matrix.

RPC contract:
  bench_categories {} -> {ok, categories:[{id, label, description,
    tools_covered:[str], runnable:bool, last:{when, pass_rate, detail_path}|null}]}
  bench_run accepts optional category:<id> (mutually exclusive with tiers/mode).

Run a category from CLI (used by the RPC spawner)::

    python3 -m tools.flywheel.bench.categories list [--json]
    python3 -m tools.flywheel.bench.categories run <id> [--mode diag|smoke]
    python3 -m tools.flywheel.bench.categories last <id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    env = os.environ.get("CHUDWARE_REPO") or os.environ.get("CHUDWARE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # tools/flywheel/bench/categories.py → repo root (parents[3])
    return Path(__file__).resolve().parents[3]


def _reports_root(repo: Path | None = None) -> Path:
    root = repo or _repo_root()
    return root / "tools" / "flywheel" / "bench" / "gym" / "reports"


def category_status_path(cat_id: str, repo: Path | None = None) -> Path:
    return _reports_root(repo) / "categories" / f"{cat_id}_status.json"


def category_report_dir(cat_id: str, repo: Path | None = None) -> Path:
    return _reports_root(repo) / "categories" / cat_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_when(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return _utc_now()


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass
class CategorySpec:
    id: str
    label: str
    description: str
    tools_covered: list[str]
    # True when the category can be launched without paid keys / exotic hardware.
    # Still may degrade honestly (SKIP) when backends unprovisioned.
    runnable: bool = True
    # How to spawn: list of argv fragments relative to repo, OR a special kind.
    # kind: "module" | "script" | "pytest" | "multi"
    kind: str = "multi"
    # For single-cmd kinds: argv after python (module) or path (script/pytest).
    argv: list[str] = field(default_factory=list)
    # multi: ordered steps as (label, argv_builder) handled in run_category.


CATEGORY_SPECS: list[CategorySpec] = [
    CategorySpec(
        id="modeling",
        label="Modeling",
        description=(
            "Design-gym C1–C15 smoke + diagnostic axes "
            "(tool_validity / vision_fix / feature_id / vc_hygiene)."
        ),
        tools_covered=[
            "new_part", "add_features", "describe_part", "measure_part",
            "check_sketch", "sketch_health", "verify_battery", "check_interference",
            "render_part", "inspect_part", "look_around", "cad_guide",
        ],
        kind="multi",
    ),
    CategorySpec(
        id="ecad",
        label="ECAD",
        description=(
            "Flight-computer (B1) and battery-charger (B2) end-to-end ECAD benches "
            "— schematic → board → sim → firmware → power budget."
        ),
        tools_covered=[
            "ecad_place_symbol", "ecad_wire", "ecad_simulate", "board_from_schematic",
            "ecad_autoroute", "ecad_run_erc", "ecad_run_drc", "ecad_run_pdn",
            "power_budget", "fw_new_project", "fw_sim_run", "ecad_antenna_design",
        ],
        kind="multi",
    ),
    CategorySpec(
        id="simulation",
        label="Simulation",
        description=(
            "simsuite FEA/CFD/EM/thermal baseline + V&V analytic-reference harness."
        ),
        tools_covered=[
            "run_fea_static", "run_fea_modal", "run_cfd_flow", "run_fea_thermal",
            "ecad_thermal_handoff", "struct_analyze",
        ],
        kind="multi",
    ),
    CategorySpec(
        id="reveng",
        label="Reverse Engineering",
        description=(
            "E2E reverse-engineering scan pipeline + datasheet ingest (pytest). "
            "Honest degradation when colmap/openmvs unprovisioned."
        ),
        tools_covered=[
            "reveng_start_session", "reveng_add_images", "reveng_reconstruct",
            "reveng_build_parametric", "reveng_ingest_datasheet",
            "reveng_attach_result",
        ],
        kind="multi",
    ),
    CategorySpec(
        id="parts_research",
        label="Parts Research",
        description=(
            "Parts-library research + datasheet look-up smoke "
            "(research_* tools, part_search/part_source, datasheet ingest)."
        ),
        tools_covered=[
            "part_search", "part_get", "part_place", "part_source", "parts_taxonomy",
            "research_web_search", "research_fetch_page", "research_material_lookup",
            "research_thread_spec", "research_standard_section", "research_datasheet_fetch",
        ],
        kind="multi",
    ),
    CategorySpec(
        id="roundtrip",
        label="Round-trip Pipeline",
        description=(
            "Parts Library research → RevEng build → Parts Library store "
            "(fixture datasheet; honest skips when backends unprovisioned)."
        ),
        tools_covered=[
            "part_search", "part_get", "part_place", "part_source",
            "reveng_start_session", "reveng_ingest_datasheet",
            "reveng_build_parametric", "reveng_attach_result",
            "research_datasheet_fetch", "match_model",
        ],
        kind="multi",
    ),
]


# Public list shaped for ``from tools.flywheel.bench.categories import CATEGORIES``.
CATEGORIES: list[dict[str, Any]] = [
    {
        "id": s.id,
        "label": s.label,
        "description": s.description,
        "tools_covered": list(s.tools_covered),
        "runnable": s.runnable,
    }
    for s in CATEGORY_SPECS
]


def get_spec(cat_id: str) -> CategorySpec | None:
    for s in CATEGORY_SPECS:
        if s.id == cat_id:
            return s
    return None


def known_ids() -> list[str]:
    return [s.id for s in CATEGORY_SPECS]


# ---------------------------------------------------------------------------
# Last-result readers (native artifacts → {when, pass_rate, detail_path})
# ---------------------------------------------------------------------------


def _last_gym_report(repo: Path) -> Path | None:
    """Most recent cycle-*.json or named report under gym/reports/ (not diag/)."""
    reports = repo / "tools" / "flywheel" / "bench" / "gym" / "reports"
    if not reports.is_dir():
        return None
    candidates: list[Path] = []
    for p in reports.glob("*.json"):
        if p.name in ("bench_status.json", "tuning_results.json"):
            continue
        # Prefer cycle / named run reports with pass_rates.
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and ("pass_rates" in data or "cells" in data):
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _read_modeling_last(repo: Path) -> dict | None:
    # Prefer category report if present, else latest gym report, else diag-latest.
    cat_summary = category_report_dir("modeling", repo) / "summary.json"
    if cat_summary.is_file():
        try:
            data = json.loads(cat_summary.read_text(encoding="utf-8"))
            return {
                "when": data.get("when") or _file_when(cat_summary),
                "pass_rate": data.get("pass_rate"),
                "executed": data.get("executed"),
                "stage": data.get("stage"),
                "detail_path": str(cat_summary),
            }
        except Exception:  # noqa: BLE001
            pass

    gym = _last_gym_report(repo)
    if gym is not None:
        try:
            data = json.loads(gym.read_text(encoding="utf-8"))
            pr = data.get("pass_rates") or {}
            overall = pr.get("overall")
            if overall is None and isinstance(pr.get("matrix"), dict):
                rates = [
                    v.get("pass_rate")
                    for v in pr["matrix"].values()
                    if isinstance(v, dict) and "pass_rate" in v
                ]
                overall = sum(rates) / len(rates) if rates else None
            return {
                "when": data.get("generated") or data.get("finished") or _file_when(gym),
                "pass_rate": overall,
                "detail_path": str(gym),
            }
        except Exception:  # noqa: BLE001
            pass

    diag = (
        repo / "tools" / "flywheel" / "bench" / "gym" / "reports" / "diag" / "diag-latest.json"
    )
    if diag.is_file():
        try:
            data = json.loads(diag.read_text(encoding="utf-8"))
            scores: list[float] = []
            for res in data.get("results") or []:
                axes = res.get("axes") or {}
                for ax in axes.values():
                    if isinstance(ax, dict) and "score" in ax:
                        scores.append(float(ax["score"]))
            rate = sum(scores) / len(scores) if scores else None
            return {
                "when": data.get("generated") or _file_when(diag),
                "pass_rate": rate,
                "detail_path": str(diag),
            }
        except Exception:  # noqa: BLE001
            pass
    return None


def _verdict_stats(summary: dict) -> tuple[float | None, int]:
    counts = summary.get("counts") or {}
    passed = int(counts.get("pass") or 0)
    warned = int(counts.get("warn") or 0)
    failed = int(counts.get("fail") or 0)
    executed = passed + warned + failed
    if executed <= 0:
        return None, 0
    return passed / executed, executed


def _parse_verdict_pass_rate(summary: dict) -> float | None:
    return _verdict_stats(summary)[0]


def _read_ecad_last(repo: Path) -> dict | None:
    cat_summary = category_report_dir("ecad", repo) / "summary.json"
    if cat_summary.is_file():
        try:
            data = json.loads(cat_summary.read_text(encoding="utf-8"))
            return {
                "when": data.get("when") or _file_when(cat_summary),
                "pass_rate": data.get("pass_rate"),
                "executed": data.get("executed"),
                "stage": data.get("stage"),
                "detail_path": str(cat_summary),
            }
        except Exception:  # noqa: BLE001
            pass

    paths = [
        repo / "benchmarks" / "flight_computer" / "summary.json",
        repo / "benchmarks" / "battery_charger" / "summary.json",
    ]
    rates: list[float] = []
    newest: Path | None = None
    newest_mtime = -1.0
    for p in paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            r = _parse_verdict_pass_rate(data)
            if r is not None:
                rates.append(r)
            mt = p.stat().st_mtime
            if mt > newest_mtime:
                newest_mtime = mt
                newest = p
        except Exception:  # noqa: BLE001
            continue
    if not rates or newest is None:
        # Fall back to VERDICT.txt presence.
        for v in (
            repo / "benchmarks" / "flight_computer" / "VERDICT.txt",
            repo / "benchmarks" / "battery_charger" / "VERDICT.txt",
        ):
            if v.is_file():
                text = v.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"RESULT:\s*(PASS|FAIL)", text)
                rate = 1.0 if m and m.group(1) == "PASS" else (0.0 if m else None)
                return {
                    "when": _file_when(v),
                    "pass_rate": rate,
                    "detail_path": str(v),
                }
        return None
    return {
        "when": _file_when(newest),
        "pass_rate": sum(rates) / len(rates),
        "detail_path": str(newest),
    }


def _read_simulation_last(repo: Path) -> dict | None:
    cat_summary = category_report_dir("simulation", repo) / "summary.json"
    if cat_summary.is_file():
        try:
            data = json.loads(cat_summary.read_text(encoding="utf-8"))
            return {
                "when": data.get("when") or _file_when(cat_summary),
                "pass_rate": data.get("pass_rate"),
                "executed": data.get("executed"),
                "stage": data.get("stage"),
                "detail_path": str(cat_summary),
            }
        except Exception:  # noqa: BLE001
            pass

    results_root = repo / "benchmarks" / "simsuite" / "results"
    latest_sim: Path | None = None
    if results_root.is_dir():
        dirs = [d for d in results_root.iterdir() if d.is_dir() and (d / "results.json").is_file()]
        if dirs:
            dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            latest_sim = dirs[0] / "results.json"

    rates: list[float] = []
    detail: Path | None = None
    when: str | None = None

    if latest_sim is not None:
        try:
            data = json.loads(latest_sim.read_text(encoding="utf-8"))
            cases = data.get("cases") or []
            executed_cases = [
                c for c in cases
                if isinstance(c, dict) and str(c.get("status", "")).lower() != "skip"
            ]
            if executed_cases:
                ok = sum(
                    1 for c in executed_cases
                    if str(c.get("status", "")).lower() in ("ok", "pass")
                )
                rates.append(ok / len(executed_cases))
            when = data.get("timestamp") or _file_when(latest_sim)
            detail = latest_sim
        except Exception:  # noqa: BLE001
            pass

    vv = repo / "reports" / "vv" / "vv_results.json"
    if vv.is_file():
        try:
            data = json.loads(vv.read_text(encoding="utf-8"))
            results = data.get("results") or data.get("cases") or []
            if isinstance(results, list) and results:
                executed = [
                    r for r in results
                    if isinstance(r, dict) and r.get("executed", True)
                ]
                if executed:
                    passed = sum(
                        1 for r in executed
                        if str(r.get("verdict", "")).upper() == "PASS"
                    )
                    rates.append(passed / len(executed))
                if detail is None:
                    detail = vv
                    when = _file_when(vv)
        except Exception:  # noqa: BLE001
            pass

    if not rates or detail is None:
        return None
    return {
        "when": when or _file_when(detail),
        "pass_rate": sum(rates) / len(rates),
        "detail_path": str(detail),
    }


def _read_pytest_summary(path: Path) -> dict | None:
    """Read a category-written pytest summary or junit-like json."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if "pass_rate" in data:
        return {
            "when": data.get("when") or _file_when(path),
            "pass_rate": data.get("pass_rate"),
            "executed": data.get("executed"),
            "stage": data.get("stage"),
            "detail_path": str(path),
        }
    # pytest --json-report style (optional)
    summary = data.get("summary") or {}
    passed = int(summary.get("passed") or 0)
    failed = int(summary.get("failed") or 0)
    errors = int(summary.get("error") or summary.get("errors") or 0)
    executed = passed + failed + errors
    if executed > 0:
        return {
            "when": data.get("created") or _file_when(path),
            "pass_rate": passed / executed,
            "executed": executed,
            "detail_path": str(path),
        }
    return {
        "when": data.get("created") or _file_when(path),
        "pass_rate": None,
        "executed": 0,
        "detail_path": str(path),
    }


def _read_reveng_last(repo: Path) -> dict | None:
    cat_summary = category_report_dir("reveng", repo) / "summary.json"
    hit = _read_pytest_summary(cat_summary)
    if hit:
        return hit
    return None


def _read_parts_research_last(repo: Path) -> dict | None:
    cat_summary = category_report_dir("parts_research", repo) / "summary.json"
    hit = _read_pytest_summary(cat_summary)
    if hit:
        return hit
    return None


def _read_roundtrip_last(repo: Path) -> dict | None:
    # Prefer category report, then dedicated roundtrip out dir.
    for p in (
        category_report_dir("roundtrip", repo) / "summary.json",
        repo / "tools" / "flywheel" / "bench" / "gym" / "reports" / "roundtrip" / "summary.json",
        repo / "benchmarks" / "roundtrip" / "summary.json",
    ):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {
                "when": data.get("when") or _file_when(p),
                "pass_rate": data.get("pass_rate"),
                "executed": data.get("executed"),
                "stage": data.get("stage"),
                "detail_path": str(p),
            }
        except Exception:  # noqa: BLE001
            continue
    return None


_LAST_READERS: dict[str, Callable[[Path], dict | None]] = {
    "modeling": _read_modeling_last,
    "ecad": _read_ecad_last,
    "simulation": _read_simulation_last,
    "reveng": _read_reveng_last,
    "parts_research": _read_parts_research_last,
    "roundtrip": _read_roundtrip_last,
}


def read_last_result(cat_id: str, repo: Path | None = None) -> dict | None:
    """Parse the category's latest result, including executed-count and stage."""
    root = repo or _repo_root()
    reader = _LAST_READERS.get(cat_id)
    if reader is None:
        return None
    try:
        hit = reader(root)
    except Exception:  # noqa: BLE001
        return None
    if not hit:
        return None
    # Normalize keys the GUI expects.
    out = {
        "when": hit.get("when"),
        "pass_rate": hit.get("pass_rate"),
        "executed": hit.get("executed"),
        "stage": hit.get("stage"),
        "detail_path": hit.get("detail_path"),
    }
    if out["detail_path"] is None:
        return None
    return out


def list_categories(repo: Path | None = None) -> list[dict[str, Any]]:
    """Full category list for the ``bench_categories`` RPC."""
    root = repo or _repo_root()
    out: list[dict[str, Any]] = []
    for s in CATEGORY_SPECS:
        last = read_last_result(s.id, root)
        out.append({
            "id": s.id,
            "label": s.label,
            "description": s.description,
            "tools_covered": list(s.tools_covered),
            "runnable": bool(s.runnable),
            "last": last,
        })
    return out


# ---------------------------------------------------------------------------
# Command builders (what bench_rpc spawns)
# ---------------------------------------------------------------------------


def build_category_cmd(cat_id: str, *, mode: str | None = None) -> list[str]:
    """Argv to run *cat_id* as a detached child of the worker.

    Always routes through this module's CLI so multi-step categories share one
    status/report writer. *mode* is only meaningful for modeling (diag|smoke).
    """
    if get_spec(cat_id) is None:
        raise KeyError(f"unknown category {cat_id!r}")
    py = sys.executable or "python3"
    cmd = [py, "-m", "tools.flywheel.bench.categories", "run", cat_id]
    if cat_id == "modeling" and mode in ("diag", "smoke"):
        cmd += ["--mode", mode]
    return cmd


# ---------------------------------------------------------------------------
# Category runners
# ---------------------------------------------------------------------------


def _run_cmd(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    logf,
    timeout: float | None = None,
) -> int:
    logf.write(f"\n$ {' '.join(cmd)}\n")
    logf.flush()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        return int(proc.returncode)
    except subprocess.TimeoutExpired:
        logf.write(f"\n# TIMEOUT after {timeout}s\n")
        return 124
    except Exception as exc:  # noqa: BLE001
        logf.write(f"\n# spawn failed: {exc}\n")
        return 1

def _run_cmd_capture(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    logf,
) -> tuple[int, str]:
    logf.write(f"\n$ {' '.join(cmd)}\n")
    logf.flush()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        logf.write(text)
        logf.flush()
        return int(proc.returncode), text
    except Exception as exc:  # noqa: BLE001
        text = f"# spawn failed: {exc}\n"
        logf.write(text)
        logf.flush()
        return 1, text


def _write_summary(out_dir: Path, payload: dict) -> Path:
    """Atomically replace ``summary.json`` with a fully serialized payload."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "summary.json"
    payload.setdefault("when", _utc_now())
    tmp = out_dir / f".summary.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _begin_invocation(out_dir: Path, category: str) -> tuple[str, Path]:
    run_id = f"{category}-{time.time_ns()}-{os.getpid()}"
    run_dir = out_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def _finish_invocation(
    out_dir: Path,
    run_dir: Path,
    run_id: str,
    payload: dict,
) -> dict:
    """Validate this invocation, persist it, then atomically publish it as latest."""
    summary = dict(payload)
    summary["run_id"] = run_id
    summary.setdefault("when", _utc_now())
    if summary.get("category") is None:
        raise ValueError("category summary missing category")
    if not isinstance(summary.get("exit_code"), int):
        raise ValueError("category summary exit_code must be an int")
    if not isinstance(summary.get("executed"), int) or summary["executed"] < 0:
        raise ValueError("category summary executed must be a non-negative int")
    if summary.get("stage") not in {"done", "skipped", "error"}:
        raise ValueError("category summary stage must be done, skipped, or error")
    rate = summary.get("pass_rate")
    if rate is not None and (
        not isinstance(rate, (int, float)) or not 0.0 <= float(rate) <= 1.0
    ):
        raise ValueError("category summary pass_rate must be null or in [0, 1]")
    executed = summary["executed"]
    stage = summary["stage"]
    exit_code = summary["exit_code"]
    if executed == 0 and rate is not None:
        raise ValueError("all-skipped summary must have null pass_rate")
    if stage == "skipped" and (executed != 0 or exit_code != 0):
        raise ValueError("skipped summary must have zero executed and exit_code 0")
    if stage == "done" and (executed == 0 or exit_code != 0 or rate is None):
        raise ValueError("done summary requires executed checks, pass_rate, and exit_code 0")
    if stage == "error" and exit_code == 0:
        raise ValueError("error summary requires non-zero exit_code")
    invocation_path = run_dir / "summary.json"
    summary["detail_path"] = str(invocation_path)
    _write_summary(run_dir, summary)
    _write_summary(out_dir, summary)
    return summary


def _pytest_cmd(targets: list[str], report_json: Path) -> list[str]:
    py = sys.executable or "python3"
    # --tb=no keeps logs small; -q for quiet. No plugins required: we parse exit code
    # and write our own summary. Optional json-report is NOT assumed.
    return [py, "-m", "pytest", *targets, "-q", "--tb=line"]


def _parse_pytest_output(text: str) -> dict[str, int]:
    """Best-effort parse of pytest -q summary line."""
    # e.g. "12 passed, 3 skipped, 1 failed in 2.3s"
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "xfailed": 0}
    m = re.search(
        r"(?:(\d+)\s+passed)?(?:,\s*)?(?:(\d+)\s+skipped)?(?:,\s*)?"
        r"(?:(\d+)\s+failed)?(?:,\s*)?(?:(\d+)\s+error)?",
        text.splitlines()[-1] if text.strip() else "",
    )
    # Prefer scanning whole text for the summary line.
    for line in reversed(text.splitlines()):
        if "passed" in line or "failed" in line or "skipped" in line or "error" in line:
            for key in counts:
                mm = re.search(rf"(\d+)\s+{key}", line)
                if mm:
                    counts[key] = int(mm.group(1))
            if any(counts.values()):
                break
    return counts


def run_modeling(repo: Path, out_dir: Path, logf, *, mode: str = "smoke") -> dict:
    run_id, run_dir = _begin_invocation(out_dir, "modeling")
    env = os.environ.copy()
    env.setdefault("CHUDWARE_REPO", str(repo))
    env.setdefault("PYTHONUNBUFFERED", "1")
    py = sys.executable or "python3"
    artifact: Path
    before: tuple[int, int] | None = None

    if mode == "diag":
        artifact = (
            repo / "tools" / "flywheel" / "bench" / "gym"
            / "reports" / "diag" / "diag-latest.json"
        )
        if artifact.is_file():
            stat = artifact.stat()
            before = (stat.st_mtime_ns, stat.st_size)
        cmd = [py, "-m", "tools.flywheel.bench.gym.cli", "diag", "--tiers", "local-24k"]
    else:
        cycle = run_id
        artifact = (
            repo / "tools" / "flywheel" / "bench" / "gym"
            / "reports" / f"{cycle}.json"
        )
        cmd = [
            py, "-m", "tools.flywheel.bench.gym.cli", "run",
            "--real", "--challenges", "C1",
            "--tiers", "local-24k",
            "--cycle", cycle,
            "--no-adjust",
        ]

    rc = _run_cmd(cmd, cwd=repo, env=env, logf=logf)
    current = artifact.is_file()
    if current and before is not None:
        stat = artifact.stat()
        current = (stat.st_mtime_ns, stat.st_size) != before

    data: dict[str, Any] | None = None
    artifact_error: str | None = None
    if current:
        try:
            loaded = json.loads(artifact.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise TypeError("report root is not an object")
            if mode != "diag" and loaded.get("cycle") != run_id:
                raise ValueError(f"report cycle {loaded.get('cycle')!r} != {run_id!r}")
            data = loaded
            copied = run_dir / f"{mode}.json"
            copied.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            artifact_error = f"invalid current artifact {artifact}: {exc}"
    else:
        artifact_error = f"no artifact created by invocation {run_id}: {artifact}"

    executed = 0
    pass_rate: float | None = None
    if data is not None and mode == "diag":
        scores = [
            float(axis["score"])
            for row in data.get("results") or []
            if isinstance(row, dict) and not row.get("error")
            for axis in (row.get("axes") or {}).values()
            if isinstance(axis, dict) and isinstance(axis.get("score"), (int, float))
        ]
        executed = len(scores)
        pass_rate = sum(scores) / executed if executed else None
    elif data is not None:
        executed = int(data.get("n_cells") or 0)
        overall = (data.get("pass_rates") or {}).get("overall")
        pass_rate = float(overall) if executed and isinstance(overall, (int, float)) else None

    exit_code = rc if rc != 0 else (1 if artifact_error else 0)
    stage = "error" if exit_code else ("skipped" if executed == 0 else "done")
    summary = {
        "category": "modeling",
        "mode": mode,
        "pass_rate": pass_rate,
        "executed": executed,
        "exit_code": exit_code,
        "stage": stage,
        "artifact_error": artifact_error,
        "steps": [{"name": f"gym_{mode}", "exit_code": rc, "cmd": cmd}],
    }
    return _finish_invocation(out_dir, run_dir, run_id, summary)


def run_ecad(repo: Path, out_dir: Path, logf) -> dict:
    run_id, run_dir = _begin_invocation(out_dir, "ecad")
    base_env = os.environ.copy()
    base_env.setdefault("CHUDWARE_REPO", str(repo))
    base_env.setdefault("PYTHONUNBUFFERED", "1")
    base_env.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
    py = sys.executable or "python3"
    scripts = [
        ("flight_computer", repo / "scripts" / "bench_flight_computer.py",
         "CHUDWARE_FC_OUT"),
        ("battery_charger", repo / "scripts" / "bench_battery_charger.py",
         "CHUDWARE_BC_OUT"),
    ]
    steps: list[dict] = []
    passed = executed = 0
    artifact_fault = False
    observed_failures: list[int] = []

    for name, script, out_var in scripts:
        if not script.is_file():
            steps.append({
                "name": name, "exit_code": None, "skipped": True,
                "reason": f"missing {script}",
            })
            continue
        step_out = run_dir / name
        env = dict(base_env)
        env[out_var] = str(step_out)
        cmd = [py, str(script)]
        rc = _run_cmd(cmd, cwd=repo, env=env, logf=logf)
        if rc != 0:
            observed_failures.append(rc)
        artifact = step_out / "summary.json"
        step: dict[str, Any] = {
            "name": name, "exit_code": rc, "cmd": cmd,
            "artifact": str(artifact),
        }
        try:
            data = json.loads(artifact.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("summary root is not an object")
            rate, step_executed = _verdict_stats(data)
            step["executed"] = step_executed
            step["pass_rate"] = rate
            counts = data.get("counts") or {}
            passed += int(counts.get("pass") or 0)
            executed += step_executed
        except Exception as exc:  # noqa: BLE001
            artifact_fault = True
            step["artifact_error"] = f"{type(exc).__name__}: {exc}"
        steps.append(step)

    pass_rate = passed / executed if executed else None
    exit_code = observed_failures[0] if observed_failures else (1 if artifact_fault else 0)
    stage = "error" if exit_code else ("skipped" if executed == 0 else "done")
    return _finish_invocation(out_dir, run_dir, run_id, {
        "category": "ecad",
        "pass_rate": pass_rate,
        "executed": executed,
        "exit_code": exit_code,
        "stage": stage,
        "steps": steps,
    })


def run_simulation(repo: Path, out_dir: Path, logf) -> dict:
    run_id, run_dir = _begin_invocation(out_dir, "simulation")
    env = os.environ.copy()
    env.setdefault("CHUDWARE_REPO", str(repo))
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
    py = sys.executable or "python3"
    steps: list[dict] = []
    passed = executed = 0
    observed_failures: list[int] = []
    artifact_fault = False

    simsuite = repo / "benchmarks" / "simsuite" / "run_all.py"
    if simsuite.is_file():
        sim_out = run_dir / "simsuite"
        cmd = [py, str(simsuite), "--quick", "--out", str(sim_out)]
        rc = _run_cmd(cmd, cwd=repo, env=env, logf=logf)
        if rc != 0:
            observed_failures.append(rc)
        artifact = sim_out / "results.json"
        step: dict[str, Any] = {
            "name": "simsuite", "exit_code": rc, "cmd": cmd,
            "artifact": str(artifact),
        }
        try:
            data = json.loads(artifact.read_text(encoding="utf-8"))
            cases = data.get("cases") or []
            current = [
                c for c in cases
                if isinstance(c, dict) and str(c.get("status", "")).lower() != "skip"
            ]
            step["executed"] = len(current)
            executed += len(current)
            n_pass = sum(
                1 for c in current
                if str(c.get("status", "")).lower() in ("ok", "pass")
            )
            passed += n_pass
            step["pass_rate"] = n_pass / len(current) if current else None
        except Exception as exc:  # noqa: BLE001
            artifact_fault = True
            step["artifact_error"] = f"{type(exc).__name__}: {exc}"
        steps.append(step)
    else:
        steps.append({
            "name": "simsuite", "exit_code": None, "skipped": True,
            "reason": "run_all.py missing",
        })

    vv = repo / "scripts" / "vv_benchmark.py"
    if vv.is_file():
        cmd = [py, "-m", "pytest", str(vv), "-q", "-k", "smoke", "--tb=line"]
        rc, text = _run_cmd_capture(cmd, cwd=repo, env=env, logf=logf)
        counts = _parse_pytest_output(text)
        step_executed = counts["passed"] + counts["failed"] + counts["error"]
        passed += counts["passed"]
        executed += step_executed
        if rc not in (0, 5):
            observed_failures.append(rc)
        steps.append({
            "name": "vv_benchmark_smoke",
            "exit_code": rc,
            "cmd": cmd,
            "pytest_counts": counts,
            "executed": step_executed,
            "pass_rate": counts["passed"] / step_executed if step_executed else None,
            "skipped": step_executed == 0 and rc in (0, 5),
        })
    else:
        steps.append({
            "name": "vv_benchmark", "exit_code": None, "skipped": True,
            "reason": "vv_benchmark.py missing",
        })

    pass_rate = passed / executed if executed else None
    exit_code = observed_failures[0] if observed_failures else (1 if artifact_fault else 0)
    stage = "error" if exit_code else ("skipped" if executed == 0 else "done")
    return _finish_invocation(out_dir, run_dir, run_id, {
        "category": "simulation",
        "pass_rate": pass_rate,
        "executed": executed,
        "exit_code": exit_code,
        "stage": stage,
        "steps": steps,
    })


def _run_pytest_bundle(
    repo: Path,
    out_dir: Path,
    logf,
    *,
    category: str,
    targets: list[str],
) -> dict:
    run_id, run_dir = _begin_invocation(out_dir, category)
    env = os.environ.copy()
    env.setdefault("CHUDWARE_REPO", str(repo))
    env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = _pytest_cmd(targets, run_dir / "pytest_report.json")
    rc, text = _run_cmd_capture(cmd, cwd=repo, env=env, logf=logf)
    (run_dir / "pytest.log").write_text(text, encoding="utf-8")

    counts = _parse_pytest_output(text)
    executed = counts["passed"] + counts["failed"] + counts["error"]
    pass_rate = counts["passed"] / executed if executed else None
    if executed == 0 and rc in (0, 5):
        exit_code = 0
        stage = "skipped"
    else:
        exit_code = rc
        stage = "done" if rc == 0 else "error"
    return _finish_invocation(out_dir, run_dir, run_id, {
        "category": category,
        "pass_rate": pass_rate,
        "executed": executed,
        "exit_code": exit_code,
        "stage": stage,
        "pytest_counts": counts,
        "targets": targets,
        "steps": [{"name": "pytest", "exit_code": rc, "cmd": cmd}],
    })


def run_reveng(repo: Path, out_dir: Path, logf) -> dict:
    targets = [
        "services/mcp/test_reveng_e2e_scan.py",
        "services/mcp/test_reveng_e2e_datasheet.py",
    ]
    existing = [t for t in targets if (repo / t).is_file()]
    if existing:
        return _run_pytest_bundle(
            repo, out_dir, logf, category="reveng", targets=existing,
        )
    run_id, run_dir = _begin_invocation(out_dir, "reveng")
    return _finish_invocation(out_dir, run_dir, run_id, {
        "category": "reveng",
        "pass_rate": None,
        "executed": 0,
        "exit_code": 0,
        "stage": "skipped",
        "steps": [{"name": "pytest", "skipped": True, "reason": "no targets"}],
    })


def run_parts_research(repo: Path, out_dir: Path, logf) -> dict:
    targets = [
        "services/research/test_research.py",
        "services/mcp/test_reveng_e2e_datasheet.py",
    ]
    existing = [t for t in targets if (repo / t).is_file()]
    extra = repo / "services" / "mcp" / "test_parts_tools_v2.py"
    if extra.is_file():
        existing.append("services/mcp/test_parts_tools_v2.py")
    if existing:
        return _run_pytest_bundle(
            repo, out_dir, logf, category="parts_research", targets=existing,
        )
    run_id, run_dir = _begin_invocation(out_dir, "parts_research")
    return _finish_invocation(out_dir, run_dir, run_id, {
        "category": "parts_research",
        "pass_rate": None,
        "executed": 0,
        "exit_code": 0,
        "stage": "skipped",
        "steps": [{"name": "pytest", "skipped": True, "reason": "no targets"}],
    })


def run_roundtrip(repo: Path, out_dir: Path, logf) -> dict:
    run_id, run_dir = _begin_invocation(out_dir, "roundtrip")
    env = os.environ.copy()
    env.setdefault("CHUDWARE_REPO", str(repo))
    env.setdefault("PYTHONUNBUFFERED", "1")
    py = sys.executable or "python3"
    script = repo / "tools" / "flywheel" / "bench" / "roundtrip_pipeline.py"
    child_out = run_dir / "pipeline"
    cmd = [py, str(script), "--out", str(child_out)]
    rc = _run_cmd(cmd, cwd=repo, env=env, logf=logf)
    child_summary = child_out / "summary.json"

    data: dict[str, Any] | None = None
    artifact_error: str | None = None
    try:
        loaded = json.loads(child_summary.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("summary root is not an object")
        data = loaded
    except Exception as exc:  # noqa: BLE001
        artifact_error = (
            f"no valid artifact created by invocation {run_id} at "
            f"{child_summary}: {type(exc).__name__}: {exc}"
        )

    pass_rate = None
    executed = 0
    if data is not None:
        pass_rate, executed = _verdict_stats(data)
    exit_code = rc if rc != 0 else (1 if artifact_error else 0)
    stage = "error" if exit_code else ("skipped" if executed == 0 else "done")
    return _finish_invocation(out_dir, run_dir, run_id, {
        "category": "roundtrip",
        "pass_rate": pass_rate,
        "executed": executed,
        "exit_code": exit_code,
        "stage": stage,
        "artifact_error": artifact_error,
        "steps": [{
            "name": "roundtrip_pipeline",
            "exit_code": rc,
            "cmd": cmd,
            "artifact": str(child_summary),
        }],
    })


_RUNNERS: dict[str, Callable[..., dict]] = {
    "modeling": run_modeling,
    "ecad": run_ecad,
    "simulation": run_simulation,
    "reveng": run_reveng,
    "parts_research": run_parts_research,
    "roundtrip": run_roundtrip,
}


def run_category(cat_id: str, *, mode: str = "smoke", repo: Path | None = None) -> int:
    """Execute a category and publish status derived only from this invocation."""
    spec = get_spec(cat_id)
    if spec is None:
        print(f"unknown category: {cat_id}", file=sys.stderr)
        print(f"known: {', '.join(known_ids())}", file=sys.stderr)
        return 2

    root = repo or _repo_root()
    out_dir = category_report_dir(cat_id, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "run.log"
    runner = _RUNNERS[cat_id]

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"# category={cat_id} when={_utc_now()}\n# repo={root}\n")
        logf.flush()
        if cat_id == "modeling":
            summary = runner(root, out_dir, logf, mode=mode)
        else:
            summary = runner(root, out_dir, logf)

    rc = int(summary["exit_code"])
    stage = str(summary["stage"])
    status = {
        "category": cat_id,
        "run_id": summary["run_id"],
        "running": False,
        "stage": stage,
        "finished": _utc_now(),
        "exit_code": rc,
        "pass_rate": summary.get("pass_rate"),
        "executed": summary["executed"],
        "last_result_path": summary["detail_path"],
        "log_path": str(log_path),
    }
    sp = category_status_path(cat_id, root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(f".{sp.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, sp)
    clean_success = rc == 0 and stage == "done"
    print(json.dumps({
        "ok": clean_success,
        "category": cat_id,
        "stage": stage,
        "pass_rate": summary.get("pass_rate"),
        "executed": summary["executed"],
        "detail_path": status["last_result_path"],
        "exit_code": rc,
    }))
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.flywheel.bench.categories")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="print category ids / RPC payload")
    p_list.add_argument("--json", action="store_true", help="full bench_categories payload")

    p_run = sub.add_parser("run", help="run one category")
    p_run.add_argument("category", choices=known_ids())
    p_run.add_argument("--mode", choices=("diag", "smoke"), default="smoke",
                       help="modeling only: diag axes or C1 smoke")

    p_last = sub.add_parser("last", help="print last result for a category")
    p_last.add_argument("category", choices=known_ids())

    args = ap.parse_args(argv)
    if args.cmd == "list":
        if args.json:
            print(json.dumps({"ok": True, "categories": list_categories()}, indent=2))
        else:
            print(json.dumps([c["id"] for c in CATEGORIES]))
        return 0
    if args.cmd == "last":
        print(json.dumps(read_last_result(args.category), indent=2))
        return 0
    if args.cmd == "run":
        return run_category(args.category, mode=args.mode)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
