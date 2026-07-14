"""Worker-RPC bridge for model rankings + per-role assignment (WS2).

Exposes ``ai_roles_get`` / ``ai_roles_set`` / ``model_rankings_get`` /
``model_rankings_set`` for the Settings GUI (BenchTab / ModelRolesAuto).

Thin wrappers over :mod:`services.agent.role_models`. Failures return
contract-shaped ``{"ok": false, "reason", "hint"}`` — never a traceback.
Registered append-only via ``WORKER_METHODS.update`` in
``engine_oracle/parts_hub/worker_rpc.py``.
"""
from __future__ import annotations

from typing import Callable


def _ai_roles_get(params: dict, _emit: Callable) -> dict:
    """Return the full ai_roles map (missing roles default to ``\"auto\"``)."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import get_ai_roles

        cfg = agent_config.load_config()
        roles = get_ai_roles(cfg)
        return {"ok": True, "roles": roles}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"ai_roles_get failed: {exc}",
            "hint": "",
        }


def _ai_roles_set(params: dict, _emit: Callable) -> dict:
    """Merge partial role assignments into config.json ai_roles."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import (
            KNOWN_ROLES,
            RoleModelError,
            assert_redaction_loopback,
            set_ai_roles,
        )

        partial = (params or {}).get("roles")
        if not isinstance(partial, dict):
            return {
                "ok": False,
                "reason": "roles mapping required",
                "hint": (
                    "Pass roles: {advisor|task|planning|default|plm|redaction: "
                    "{provider, model} | \"auto\"}"
                ),
            }

        # HARD floor at set-time for redaction fixed assignments.
        if "redaction" in partial:
            val = partial["redaction"]
            if isinstance(val, dict):
                prov = str(val.get("provider") or "").strip()
                if prov:
                    try:
                        assert_redaction_loopback(prov)
                    except RoleModelError as exc:
                        return {
                            "ok": False,
                            "reason": str(exc),
                            "hint": (
                                "redaction must use a loopback provider "
                                "(llamacpp/lmstudio/ollama/vllm)."
                            ),
                        }

        cfg = agent_config.load_config()
        roles = set_ai_roles(partial, cfg, persist=True)
        return {"ok": True, "roles": roles, "known_roles": list(KNOWN_ROLES)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"ai_roles_set failed: {exc}",
            "hint": "",
        }


def _model_rankings_get(params: dict, _emit: Callable) -> dict:
    """Return merged rankings {model_id: {rank, source, notes, ...}}."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import rankings_as_dict

        cfg = agent_config.load_config()
        rankings = rankings_as_dict(cfg)
        return {"ok": True, "rankings": rankings}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"model_rankings_get failed: {exc}",
            "hint": "",
        }


def _model_rankings_set(params: dict, _emit: Callable) -> dict:
    """Merge user ranking overrides into config.json model_rankings."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import set_user_rankings

        partial = (params or {}).get("rankings")
        if not isinstance(partial, dict):
            return {
                "ok": False,
                "reason": "rankings mapping required",
                "hint": (
                    "Pass rankings: {model_id: {rank: 1-5, notes?: str} | rank_int}"
                ),
            }
        cfg = agent_config.load_config()
        rankings = set_user_rankings(partial, cfg, persist=True)
        return {"ok": True, "rankings": rankings}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"model_rankings_set failed: {exc}",
            "hint": "",
        }


ROLE_MODELS_METHODS: dict[str, Callable] = {
    "ai_roles_get": _ai_roles_get,
    "ai_roles_set": _ai_roles_set,
    "model_rankings_get": _model_rankings_get,
    "model_rankings_set": _model_rankings_set,
}
