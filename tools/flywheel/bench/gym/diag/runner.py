"""Run all diagnostic axes for one or more tiers and persist results."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tool_validity import run_tool_validity
from .vision_fix import run_vision_fix
from .feature_id import run_feature_id
from .vc_hygiene import run_vc_hygiene

DIAG_AXES = ("tool_validity", "vision_fix", "feature_id", "vc_hygiene")

_HERE = Path(__file__).resolve().parent
REPORTS_DIR = _HERE.parent / "reports"
DIAG_DIR = REPORTS_DIR / "diag"


def _provider_for_tier(tier: str):
    """Resolve provider the same way gym cells do."""
    from tools.flywheel.bench.gym.runner import _provider_for_tier as gym_provider
    return gym_provider(tier)


def _model_meta(tier: str, provider: Any) -> dict[str, Any]:
    from services.agent.tiers import LOCAL_TIER_MODELS, parse_openrouter_tier, load_local_models

    or_model = parse_openrouter_tier(tier)
    if or_model is not None:
        return {
            "id": or_model,
            "label": or_model.split("/")[-1].replace("-", " ").title(),
            "provider": "openrouter",
            "kind": "paid",
            "tier": tier,
        }
    if tier.startswith("local"):
        try:
            fanout = load_local_models().get("fanout") or "qwen3-4b"
        except Exception:  # noqa: BLE001
            fanout = "qwen3-4b"
        model_id = LOCAL_TIER_MODELS.get(tier) or fanout
        return {
            "id": model_id,
            "label": model_id,
            # Local tiers always report llamacpp (mock injection is a test stand-in).
            "provider": "llamacpp",
            "kind": "local",
            "tier": tier,
        }
    return {
        "id": getattr(provider, "model", None) or tier,
        "label": tier,
        "provider": getattr(provider, "name", "unknown") or "unknown",
        "kind": "paid" if not tier.startswith("local") else "local",
        "tier": tier,
    }


def run_diag(
    tiers: list[str],
    *,
    seed: int = 1,
    axes: list[str] | None = None,
    provider_factory=None,
) -> dict[str, Any]:
    """Run diagnostic suites for each tier.

    *provider_factory(tier) -> Provider* is injectable for tests (mock provider).
    Returns a results dict ready for write_diag_results / leaderboard aggregation.
    """
    wanted = list(axes) if axes else list(DIAG_AXES)
    runners = {
        "tool_validity": run_tool_validity,
        "vision_fix": run_vision_fix,
        "feature_id": run_feature_id,
        "vc_hygiene": run_vc_hygiene,
    }
    factory = provider_factory or _provider_for_tier
    per_tier: list[dict[str, Any]] = []
    any_mock = False

    for tier in tiers:
        t0 = time.time()
        try:
            provider = factory(tier)
        except Exception as exc:  # noqa: BLE001 — fail closed per tier
            per_tier.append({
                "tier": tier,
                "error": f"{type(exc).__name__}: {exc}",
                "axes": {},
                "model": {"id": tier, "label": tier, "provider": "error",
                          "kind": "local", "tier": tier},
            })
            continue

        pname = getattr(provider, "name", "") or ""
        if pname in ("mock", "mock-roles", "base"):
            any_mock = True

        meta = _model_meta(tier, provider)
        axis_results: dict[str, Any] = {}
        for axis in wanted:
            fn = runners.get(axis)
            if fn is None:
                continue
            axis_results[axis] = fn(provider, seed=seed)

        per_tier.append({
            "tier": tier,
            "model": meta,
            "axes": axis_results,
            "elapsed_s": round(time.time() - t0, 3),
            "supports_vision": bool(getattr(provider, "supports_vision", True)),
        })

    return {
        "schema": "aicad.diag/1",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": seed,
        "axes": wanted,
        "tiers": list(tiers),
        "mock": any_mock,
        "results": per_tier,
    }


def write_diag_results(results: dict[str, Any], out_dir: Path | None = None) -> Path:
    """Persist diag results under reports/diag/. Returns the written path."""
    out = Path(out_dir) if out_dir is not None else DIAG_DIR
    out.mkdir(parents=True, exist_ok=True)
    ts = results.get("generated", "now").replace(":", "").replace("-", "")
    # One file per build (latest) + a timestamped archive copy.
    latest = out / "diag-latest.json"
    latest.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    archive = out / f"diag-{ts}.json"
    if not archive.exists():
        archive.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return latest
