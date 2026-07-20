"""design_recipe — NL intent → ordered tool recipe steps (aicad.recipe/1).

Data: services/agent/recipes/<id>.yaml
Matching: case-insensitive keyword overlap over title+keywords+summary.
Empty query returns the full catalog.
"""
from __future__ import annotations

import pathlib
from typing import Any

_RECIPES_DIR = pathlib.Path(__file__).resolve().parent / "recipes"
_CACHE: list[dict[str, Any]] | None = None


def _load_recipes(force: bool = False) -> list[dict[str, Any]]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    out: list[dict[str, Any]] = []
    if _RECIPES_DIR.is_dir():
        for path in sorted(_RECIPES_DIR.glob("*.yaml")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(text) or {}
            except Exception:  # noqa: BLE001 — no PyYAML: minimal parse
                data = _minimal_yaml(text)
            if not isinstance(data, dict):
                continue
            rid = str(data.get("id") or path.stem).strip()
            if not rid:
                continue
            data["id"] = rid
            out.append(data)
    _CACHE = out
    return out


def _minimal_yaml(text: str) -> dict[str, Any]:
    """Tiny fallback when PyYAML is absent — enough for our flat recipe files."""
    data: dict[str, Any] = {}
    keywords: list[str] = []
    domains: list[str] = []
    steps: list[dict[str, Any]] = []
    verify: list[str] = []
    cur_list: str | None = None
    cur_step: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - tool:"):
            if cur_step:
                steps.append(cur_step)
            cur_step = {"tool": line.split(":", 1)[1].strip().strip("\"'"), "params": {}, "note": ""}
            cur_list = "steps"
            continue
        if line.startswith("    note:") and cur_step is not None:
            cur_step["note"] = line.split(":", 1)[1].strip().strip("\"'")
            continue
        if line.startswith("  - ") and cur_list == "verify":
            verify.append(line[4:].strip().strip("\"'"))
            continue
        if line.startswith("  - ") and cur_list == "keywords":
            keywords.append(line[4:].strip().strip("\"'"))
            continue
        if line.startswith("  - ") and cur_list == "domains":
            domains.append(line[4:].strip().strip("\"'"))
            continue
        if ":" in line and not line.startswith(" "):
            if cur_step and cur_list == "steps":
                steps.append(cur_step)
                cur_step = None
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip("\"'")
            if key in ("keywords", "domains", "steps", "verify"):
                cur_list = key
                if key == "steps" and val not in ("", "[]"):
                    pass
                continue
            cur_list = None
            if val:
                data[key] = val
            continue
    if cur_step:
        steps.append(cur_step)
    if keywords:
        data["keywords"] = keywords
    if domains:
        data["domains"] = domains
    if steps:
        data["steps"] = steps
    if verify:
        data["verify"] = verify
    return data


def _score(query: str, recipe: dict[str, Any]) -> int:
    q = query.lower()
    if not q.strip():
        return 0
    blob_parts = [
        str(recipe.get("title") or ""),
        str(recipe.get("summary") or ""),
        " ".join(str(k) for k in (recipe.get("keywords") or [])),
        " ".join(str(d) for d in (recipe.get("domains") or [])),
    ]
    blob = " ".join(blob_parts).lower()
    score = 0
    for tok in q.replace(",", " ").split():
        tok = tok.strip()
        if len(tok) < 2:
            continue
        if tok in blob:
            score += 2 if len(tok) > 3 else 1
    for kw in recipe.get("keywords") or []:
        k = str(kw).lower()
        if k and k in q:
            score += 3
    return score


def design_recipe(query: str = "", max_results: int = 3) -> dict[str, Any]:
    """Return matching recipes for *query*, or the full catalog when blank."""
    recipes = _load_recipes()
    q = (query or "").strip()
    max_n = max(1, min(20, int(max_results or 3)))

    def _public(r: dict[str, Any]) -> dict[str, Any]:
        steps_out = []
        for s in r.get("steps") or []:
            if not isinstance(s, dict):
                continue
            steps_out.append({
                "tool": s.get("tool"),
                "params": s.get("params") or {},
                "note": s.get("note") or "",
            })
        return {
            "id": r.get("id"),
            "title": r.get("title") or r.get("id"),
            "summary": r.get("summary") or "",
            "steps": steps_out,
            "verify": list(r.get("verify") or []),
        }

    if not q:
        return {
            "ok": True,
            "recipes": [
                {
                    "id": r.get("id"),
                    "title": r.get("title") or r.get("id"),
                    "summary": r.get("summary") or "",
                }
                for r in recipes
            ],
        }

    ranked = sorted(
        (( _score(q, r), r) for r in recipes),
        key=lambda t: (-t[0], str(t[1].get("id") or "")),
    )
    picked = [_public(r) for sc, r in ranked if sc > 0][:max_n]
    if not picked and ranked:
        # Always return something useful — top by id order if no score.
        picked = [_public(r) for _, r in ranked[:max_n]]
    return {"ok": True, "recipes": picked}
