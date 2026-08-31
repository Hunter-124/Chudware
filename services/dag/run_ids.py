"""Safe DAG run identifiers shared by every on-disk run surface."""
from __future__ import annotations

import re

MAX_RUN_ID_LENGTH = 128
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` unchanged when it is one safe path component.

    Run ids are caller-visible stable identifiers, so invalid values are
    rejected rather than sanitized into a potentially colliding identifier.
    """
    if not isinstance(run_id, str):
        raise ValueError("run_id must be a string")
    if not run_id:
        raise ValueError("run_id must not be empty")
    if len(run_id) > MAX_RUN_ID_LENGTH:
        raise ValueError(f"run_id must be at most {MAX_RUN_ID_LENGTH} characters")
    if run_id in {".", ".."}:
        raise ValueError("run_id must not be a dot path component")
    if "/" in run_id or "\\" in run_id:
        raise ValueError("run_id must not contain path separators")
    if _SAFE_RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(
            "run_id contains unsafe characters; use an ASCII letter or digit first, "
            "then only letters, digits, '_', '-' and '.'"
        )
    return run_id
