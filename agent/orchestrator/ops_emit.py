"""Extract + validate the modeler/fixer's doc-ops array from a model reply.

The modeler emits a JSON array of ops (docs/contracts/doc_ops.md). This module parses that array
out of the reply (a fenced ```json block first, then a bare top-level ``[...]`` fallback, mirroring
``loop._extract_code``), and dry-run-validates it by building the WOULD-BE document with
``engine.doc.ops.apply_ops`` against the live doc — so an invalid batch is caught BEFORE it touches
the session.

The machine drives the retry/escape-hatch policy; this module just reports parse + validation
outcomes:
  * :func:`extract_ops` -> ``(ops | None, parse_error | None)``.
  * :func:`dry_run` -> ``(ok, error_text | None)`` against a document dict.
  * :func:`extract_revert` -> True if the reply is a ``{"revert": true}`` verdict (fixer).
  * :func:`script_escape_op` -> the single ``add_feature{type:"script"}`` op (the proven floor).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_JSON_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def _first_balanced(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    """Return the first balanced ``open_ch..close_ch`` span in *text* (depth-tracked), or None."""
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _try_load(blob: str) -> Any:
    try:
        return json.loads(blob)
    except Exception:  # noqa: BLE001
        return _SENTINEL


_SENTINEL = object()


def extract_ops(reply: str) -> tuple[Optional[list], Optional[str]]:
    """Pull a JSON ops ARRAY out of *reply*.

    Tries each fenced block (parsing an array directly, or an object with an ``ops`` array), then a
    bare top-level ``[...]``. Returns ``(ops, None)`` on success or ``(None, error)`` describing why
    no array was found.
    """
    candidates: list[str] = [m.group(1).strip() for m in _JSON_FENCE_RE.finditer(reply or "")]
    bare = _first_balanced(reply or "", "[", "]")
    if bare:
        candidates.append(bare)

    last_err = "no JSON ops array found in reply"
    for blob in candidates:
        data = _try_load(blob)
        if data is _SENTINEL:
            last_err = "ops block was not valid JSON"
            continue
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict) and isinstance(data.get("ops"), list):
            return data["ops"], None
        last_err = "JSON parsed but was not an ops array"
    return None, last_err


def extract_revert(reply: str) -> bool:
    """True if the fixer replied with a ``{"revert": true}`` verdict."""
    for m in _JSON_FENCE_RE.finditer(reply or ""):
        data = _try_load(m.group(1).strip())
        if isinstance(data, dict) and data.get("revert") is True:
            return True
    obj = _first_balanced(reply or "", "{", "}")
    if obj:
        data = _try_load(obj)
        if isinstance(data, dict) and data.get("revert") is True:
            return True
    return False


def dry_run(doc: dict, ops: list) -> tuple[bool, Optional[str]]:
    """Validate *ops* by building the would-be document. Returns ``(ok, error_text)``.

    Uses ``engine.doc.ops.apply_ops`` which runs the SAME schema + referential validation the
    session applies, so a clean dry-run means the real ``session.apply_ops`` will accept the batch.
    """
    from engine.doc.ops import OpError, apply_ops

    try:
        apply_ops(doc, ops)
        return True, None
    except OpError as e:
        env = e.to_envelope()
        detail = env.get("detail", {})
        all_errs = detail.get("all_errors")
        reason = env.get("message")
        if all_errs:
            reason = reason + " | " + "; ".join(all_errs[:5])
        return False, reason
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def script_escape_op(code: str, name: str = "Script") -> dict:
    """The escape-hatch op: a single ``add_feature{type:"script"}`` carrying a build123d script.

    The proven floor (legacy ``agent/loop.py``): when the modeler can't produce valid ops twice, we
    ask it for a script instead and wrap that one body in a ``script`` feature.
    """
    return {
        "op": "add_feature",
        "feature": {"type": "script", "name": name, "params": {"code": code}, "refs": {}},
    }


_PY_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_SCRIPTISH_RE = re.compile(r"^\s*(?:result|parts|assembly)\s*=", re.M)


def extract_script(reply: str) -> Optional[str]:
    """Pull a build123d script (assigns ``result``/``parts``) out of *reply* for the escape hatch."""
    for m in _PY_FENCE_RE.finditer(reply or ""):
        body = m.group(1).strip()
        if _SCRIPTISH_RE.search(body):
            return body
    # Bare fallback: a fenced block of any tag that looks scriptish.
    for m in re.finditer(r"```[A-Za-z0-9_+-]*\s*\n(.*?)```", reply or "", re.DOTALL):
        body = m.group(1).strip()
        if _SCRIPTISH_RE.search(body):
            return body
    return None
