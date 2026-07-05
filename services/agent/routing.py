"""Model routing & sources — schema ``aicad.routing/1`` (contracts/advisor.md §14).

This is the **single validation authority** for ``services/agent/routing.yaml``.
The GUI settings panel (T-W17) reads and writes routing *through this module*
(via the ``get``/``set`` CLI), never as free text — so schema rules and role→model
resolution live in exactly one place.

Responsibilities (kept minimal on purpose; the T-W14 driver EXTENDS this module
with live provider dispatch / delegation-policy wiring — it must not fork a
parallel one):

* :func:`load` / :func:`loads` — parse YAML, run :func:`validate`.
* :func:`validate` — structural + semantic checks (raises :class:`RoutingError`).
* :func:`resolve` — given a role, return the concrete ``(model_id, source_id)``
  the preference list resolves to against the configured sources, or ``None`` if
  no preference is currently satisfiable.
* :func:`warnings_for` — non-blocking advisories on risky choices (e.g. overseer
  on a small local model). Warn, never block (Hunter directive).
* :func:`view` — the JSON object the GUI renders: sources + assignments + the
  per-role *resolved* model + warnings. Also the T-W14 handoff contract.
* :func:`dump` / :func:`save` — validated write-back (round-trips through
  :func:`validate` before touching disk).
* CLI: ``python3 -m services.agent.routing get`` prints the ``view`` JSON;
  ``... set`` reads a routing model as JSON on stdin, validates, writes YAML,
  and prints the resulting ``view``. This is the seam the C++ GUI shells.

The symbolic tiers ``strongest`` / ``strong`` / ``cheap_local`` / ``cheapest``
are resolved against per-model ``tier`` labels; the mapping is deliberately data
(``_TIER_RANK``) so no role→model binding is hardcoded.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dep of the agent stack
    yaml = None  # type: ignore

SCHEMA = "aicad.routing/1"

# Source kinds allowed by §14. Consumer-OAuth kinds are intentionally absent
# (Pillar 12: no consumer-OAuth reuse). The GUI offers exactly this set.
ALLOWED_SOURCE_KINDS = (
    "anthropic",
    "openai",
    "openrouter",
    "lmstudio",
    "llamacpp",
    "ollama",
)

# Symbolic preference tokens the GUI drag-orders and resolve() understands. A
# token is either one of these or a concrete "<model_id>@<source_id>" pin.
SYMBOLIC_TOKENS = ("strongest", "strong", "cheap_local", "cheapest")

# Ordering of concrete model tiers from strongest to cheapest. Sources declare a
# per-model ``tier`` from this vocabulary; resolve() maps a symbolic token to the
# best matching live model. Higher rank == stronger.
_TIER_RANK = {
    "frontier": 40,
    "strong": 30,
    "local_large": 20,
    "local": 10,
    "cheap": 5,
}

# Kinds that run on the user's own machine (used to satisfy *_local tokens and to
# flag "overseer on a small local model" style warnings).
_LOCAL_KINDS = ("lmstudio", "llamacpp", "ollama")

# Default routing file location (repo-relative). GUI callers may pass an explicit
# path; tests use tmp paths.
DEFAULT_PATH = Path(__file__).resolve().parent / "routing.yaml"

# Roles whose model choice materially gates mission quality; a weak (local/small)
# pick here earns a non-blocking warning. This list is advisory-only — it never
# hardcodes a model, only which roles to caution about.
_QUALITY_CRITICAL_ROLES = ("overseer", "orchestrator", "reviewer", "fixer")


class RoutingError(ValueError):
    """Raised on any schema violation. Message is user-facing (shown inline)."""


# ---------------------------------------------------------------------------
# Load / validate
# ---------------------------------------------------------------------------

def load(path: str | Path | None = None) -> dict[str, Any]:
    """Read + validate the routing table at ``path`` (default: routing.yaml)."""
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.exists():
        raise RoutingError(f"routing file not found: {p}")
    return loads(p.read_text(encoding="utf-8"))


def loads(text: str) -> dict[str, Any]:
    """Parse YAML ``text`` and validate it. Returns the normalized table."""
    if yaml is None:  # pragma: no cover
        raise RoutingError("PyYAML is required to parse routing.yaml")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
        raise RoutingError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RoutingError("routing document must be a mapping")
    validate(data)
    return data


def from_obj(obj: Any) -> dict[str, Any]:
    """Validate an in-memory routing model (e.g. decoded from GUI JSON)."""
    if not isinstance(obj, dict):
        raise RoutingError("routing model must be an object")
    validate(obj)
    return obj


def validate(data: dict[str, Any]) -> None:
    """Structural + semantic validation. Raises :class:`RoutingError` on any issue.

    Rules enforced (§14):
      * schema tag present and correct
      * sources: list of {id, kind∈ALLOWED, models:[{id, tier}]}; unique ids
      * assignments: mapping role→non-empty preference list; each token is a
        known symbolic token OR a "model@source" pin whose source exists
      * a ``default`` assignment is required (fallback for unlisted roles)
      * approval_policy: {depth_1, deeper} with allowed values
    """
    if data.get("schema") != SCHEMA:
        raise RoutingError(f"schema must be '{SCHEMA}', got {data.get('schema')!r}")

    sources = data.get("sources")
    if not isinstance(sources, list):
        raise RoutingError("'sources' must be a list")
    seen_ids: set[str] = set()
    source_ids: set[str] = set()
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            raise RoutingError(f"sources[{i}] must be a mapping")
        sid = src.get("id")
        if not isinstance(sid, str) or not sid:
            raise RoutingError(f"sources[{i}] missing non-empty 'id'")
        if sid in seen_ids:
            raise RoutingError(f"duplicate source id '{sid}'")
        seen_ids.add(sid)
        source_ids.add(sid)
        kind = src.get("kind")
        if kind not in ALLOWED_SOURCE_KINDS:
            raise RoutingError(
                f"source '{sid}' kind '{kind}' not allowed; "
                f"pick one of {', '.join(ALLOWED_SOURCE_KINDS)}"
            )
        models = src.get("models")
        if not isinstance(models, list) or not models:
            raise RoutingError(f"source '{sid}' must list at least one model")
        for j, m in enumerate(models):
            if not isinstance(m, dict) or not m.get("id"):
                raise RoutingError(f"source '{sid}' models[{j}] missing 'id'")
            tier = m.get("tier")
            if tier is not None and tier not in _TIER_RANK:
                raise RoutingError(
                    f"source '{sid}' model '{m.get('id')}' tier '{tier}' unknown; "
                    f"use one of {', '.join(_TIER_RANK)}"
                )

    assignments = data.get("assignments")
    if not isinstance(assignments, dict) or not assignments:
        raise RoutingError("'assignments' must be a non-empty mapping of role→prefs")
    if "default" not in assignments:
        raise RoutingError("'assignments' must include a 'default' fallback row")
    for role, prefs in assignments.items():
        if not isinstance(prefs, list) or not prefs:
            raise RoutingError(f"assignment for role '{role}' must be a non-empty list")
        for tok in prefs:
            if not isinstance(tok, str) or not tok:
                raise RoutingError(f"role '{role}' has an empty preference token")
            if tok in SYMBOLIC_TOKENS:
                continue
            # concrete pin "model@source"
            if "@" in tok:
                _, _, pin_src = tok.partition("@")
                if pin_src not in source_ids:
                    raise RoutingError(
                        f"role '{role}' pins source '{pin_src}' which is not configured"
                    )
                continue
            raise RoutingError(
                f"role '{role}' token '{tok}' is neither a symbolic tier "
                f"({', '.join(SYMBOLIC_TOKENS)}) nor a 'model@source' pin"
            )

    policy = data.get("approval_policy", {})
    if not isinstance(policy, dict):
        raise RoutingError("'approval_policy' must be a mapping")
    for key, allowed in (("depth_1", ("auto", "overseer", "human")),
                         ("deeper", ("auto", "overseer", "human"))):
        if key in policy and policy[key] not in allowed:
            raise RoutingError(
                f"approval_policy.{key} '{policy[key]}' invalid; "
                f"use one of {', '.join(allowed)}"
            )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _iter_models(data: dict[str, Any]):
    """Yield (source_id, kind, model_id, tier, rank) over all configured models."""
    for src in data.get("sources", []):
        sid = src.get("id", "")
        kind = src.get("kind", "")
        for m in src.get("models", []):
            tier = m.get("tier")
            rank = _TIER_RANK.get(tier, 0)
            yield sid, kind, m.get("id", ""), tier, rank


def _resolve_token(data: dict[str, Any], token: str):
    """Resolve one preference token to (model_id, source_id) or None if unmet."""
    models = list(_iter_models(data))
    if not models:
        return None

    if "@" in token and token not in SYMBOLIC_TOKENS:
        model_id, _, source_id = token.partition("@")
        for sid, _kind, mid, _tier, _rank in models:
            if sid == source_id and mid == model_id:
                return (mid, sid)
        return None

    if token in ("strongest", "strong"):
        # strongest = global max rank; 'strong' = same selection (best available
        # non-frontier preferred but we take the best that exists — a single
        # source collapses everything, per §14).
        best = max(models, key=lambda t: t[4])
        return (best[2], best[0]) if best[4] > 0 or best[2] else None

    if token in ("cheapest", "cheap_local"):
        if token == "cheap_local":
            local = [m for m in models if m[1] in _LOCAL_KINDS]
            pool = local or []
        else:
            pool = list(models)
        if not pool:
            return None
        cheapest = min(pool, key=lambda t: t[4] if t[4] > 0 else 99)
        return (cheapest[2], cheapest[0])

    return None


def resolve(data: dict[str, Any], role: str):
    """Return (model_id, source_id) the role resolves to, or None.

    Walks the role's preference list (falling back to ``default``), returning the
    first token that resolves against the current sources.
    """
    assignments = data.get("assignments", {})
    prefs = assignments.get(role) or assignments.get("default") or []
    for token in prefs:
        hit = _resolve_token(data, token)
        if hit is not None:
            return hit
    return None


def warnings_for(data: dict[str, Any], role: str) -> list[str]:
    """Non-blocking advisories for a role's current resolution. Warn, never block."""
    out: list[str] = []
    hit = resolve(data, role)
    if hit is None:
        out.append("no configured source satisfies this preference — role would fall back")
        return out
    model_id, source_id = hit
    # Find the resolved model's kind + rank.
    for sid, kind, mid, _tier, rank in _iter_models(data):
        if sid == source_id and mid == model_id:
            if role in _QUALITY_CRITICAL_ROLES and kind in _LOCAL_KINDS and rank <= _TIER_RANK["local"]:
                out.append(
                    f"'{role}' resolves to a small local model ({model_id}); "
                    "quality here gates the whole mission — consider a stronger source"
                )
            break
    return out


# ---------------------------------------------------------------------------
# View (GUI + T-W14 handoff contract)
# ---------------------------------------------------------------------------

def view(data: dict[str, Any]) -> dict[str, Any]:
    """The JSON the GUI renders and the T-W14 driver consumes.

    Shape::

        {
          "schema": "aicad.routing/1",
          "sources": [ ...as configured... ],
          "assignments": {role: [prefs]},
          "resolved": {role: {"model": "...", "source": "...", "warnings": [...]}},
          "approval_policy": {...}
        }

    ``resolved`` shows what each preference list *currently* means given the
    configured sources, so the user sees the consequence of every edit.
    """
    resolved: dict[str, Any] = {}
    for role in data.get("assignments", {}):
        hit = resolve(data, role)
        resolved[role] = {
            "model": hit[0] if hit else None,
            "source": hit[1] if hit else None,
            "warnings": warnings_for(data, role),
        }
    return {
        "schema": SCHEMA,
        "sources": data.get("sources", []),
        "assignments": data.get("assignments", {}),
        "resolved": resolved,
        "approval_policy": data.get("approval_policy", {}),
    }


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

def dump(data: dict[str, Any]) -> str:
    """Validate then serialize to YAML text (does not touch disk)."""
    validate(data)
    if yaml is None:  # pragma: no cover
        raise RoutingError("PyYAML is required to write routing.yaml")
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def save(data: dict[str, Any], path: str | Path | None = None) -> Path:
    """Validate + write ``data`` to ``path`` (default routing.yaml). Returns path."""
    p = Path(path) if path is not None else DEFAULT_PATH
    text = dump(data)  # validates first — a bad model never reaches disk
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# CLI (the seam the C++ GUI shells)
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python3 -m services.agent.routing")
    ap.add_argument("cmd", choices=("get", "set", "validate"))
    ap.add_argument("--path", default=None, help="routing.yaml path (default: shipped)")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "get":
            data = load(args.path)
            print(json.dumps(view(data)))
            return 0
        if args.cmd == "validate":
            load(args.path)
            print(json.dumps({"ok": True}))
            return 0
        # set: read a routing model as JSON on stdin, validate, write, echo view.
        raw = sys.stdin.read()
        obj = json.loads(raw)
        # The GUI may send either a full routing doc or a {"sources","assignments",
        # "approval_policy"} subset; normalize to a doc with the schema tag.
        doc = {
            "schema": SCHEMA,
            "sources": obj.get("sources", []),
            "assignments": obj.get("assignments", {}),
        }
        if "approval_policy" in obj:
            doc["approval_policy"] = obj["approval_policy"]
        validate(doc)
        save(doc, args.path)
        print(json.dumps(view(doc)))
        return 0
    except RoutingError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {exc}"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
