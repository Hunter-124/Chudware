"""Role registry + editable-prompt resolution.

Each role (intake, planner, modeler, reviewer, fixer, memory) has a shipped markdown prompt in
``agent/prompts/<role>.md``. A user override at ``%APPDATA%\\AI-CAD\\prompts\\<role>.md`` takes
precedence (the GUI prompt editor writes there). The modeler/fixer prompts get the shared ops
cheatsheet (``agent/prompts/shared/ops_cheatsheet.md``) appended so the model always has the format
+ few-shot examples in-context.

``get_prompts_v2`` returns the per-role records the GUI editor renders (built-in text, override
path, resolved text, overridden flag) — the worker exposes it verbatim.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from engine import paths

ROLES = ("intake", "planner", "modeler", "reviewer", "fixer", "memory")

# Roles that get the ops cheatsheet appended (they emit doc ops).
_OPS_ROLES = ("modeler", "fixer")

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SHARED_DIR = _PROMPTS_DIR / "shared"


def _override_dir() -> Path:
    return paths.ROOT / "prompts"


def builtin_path(role: str) -> Path:
    return _PROMPTS_DIR / f"{role}.md"


def override_path(role: str) -> Path:
    return _override_dir() / f"{role}.md"


def _read(path: Path, attempts: int = 3) -> str:
    """Read a prompt file with a short retry (Desktop repos see transient AV/sync locks).

    A silent empty return here once misrouted every downstream role decision — so the
    final failure is loud at the call site (load_prompt raises on an empty builtin).
    """
    import time as _time

    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            if i + 1 < attempts:
                _time.sleep(0.05)
    return ""


def _cheatsheet() -> str:
    return _read(_SHARED_DIR / "ops_cheatsheet.md")


def load_prompt(role: str) -> tuple[str, str]:
    """Resolve a role's system prompt.

    Returns ``(text, source)`` where ``source`` is the absolute path the text came from
    (the override if present and non-empty, else the built-in). The ops cheatsheet is appended
    for ops-emitting roles so every modeler/fixer call carries the format examples.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    ov = override_path(role)
    ov_text = _read(ov).strip() if ov.exists() else ""
    if ov_text:
        text, source = ov_text, str(ov)
    else:
        text, source = _read(builtin_path(role)), str(builtin_path(role))
        if not text.strip():
            raise RuntimeError(f"role prompt unreadable/empty: {source}")
    if role in _OPS_ROLES:
        cs = _cheatsheet()
        if cs:
            text = text.rstrip() + "\n\n---\n" + cs
    return text, source


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def get_prompts_v2() -> list[dict]:
    """Per-role prompt records for the GUI editor (diff vs built-in, reset).

    Each record: ``{role, path, override_path, text, overridden}`` — ``text`` is the resolved
    prompt (override if present, else built-in; cheatsheet NOT appended here so the editor shows
    the editable source), ``overridden`` flags whether an override is active.
    """
    out: list[dict] = []
    for role in ROLES:
        bpath = builtin_path(role)
        opath = override_path(role)
        ov_text = _read(opath).strip() if opath.exists() else ""
        overridden = bool(ov_text)
        text = ov_text if overridden else _read(bpath)
        out.append({
            "role": role,
            "path": str(bpath),
            "override_path": str(opath),
            "text": text,
            "overridden": overridden,
        })
    return out
