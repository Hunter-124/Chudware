"""services.plm.summarize — doc_diff → plain-language change summary.

The narration layer that makes the timeline read like a story (plan §0.5, §1):
every history entry gets a one-line, plain-language summary derived from the
semantic ``doc_diff`` output.

DETERMINISTIC-FIRST (Pillar 13, normative for this module):

* The summary is TEMPLATED from the structured ``doc_diff`` fields.
* Every numeral in the summary is COPIED from the diff output — never invented,
  never paraphrased. Provable: :func:`numeral_tokens` of the summary is a
  subset of :func:`numeral_tokens` of ``json.dumps(diff)`` (tested).
* Feature display labels come from the documents ONLY when they carry no
  digits; otherwise the feature id (which appears verbatim in the diff) is
  used, so label text can never smuggle in a numeral absent from the diff.
* Any learned polish is optional garnish behind ``CHUDWARE_PLM_SUMMARY_POLISH=1``
  plus an explicitly registered hook; its output is validated (numeral subset
  + no git vocabulary) and discarded in favor of the deterministic text on any
  violation. With the flag unset this module never calls anything learned.

Ease-of-use law (contract §8.1): no git vocabulary in any produced string.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

# Forbidden user-facing words (contract §8.1). Mirrors plm_bridge._GIT_VOCAB.
_GIT_VOCAB = frozenset({
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
})

# Cap the number of clauses so a huge edit session still reads like a sentence.
_MAX_CLAUSES = 6

_NUMERAL_RE = re.compile(r"\d+(?:\.\d+)?")

# Optional learned-polish hook (garnish only; see module docstring).
_POLISH_FLAG = "CHUDWARE_PLM_SUMMARY_POLISH"
_polish_hook: Callable[[str, dict], str] | None = None


def set_polish_hook(fn: Callable[[str, dict], str] | None) -> None:
    """Register (or clear) the optional learned-polish hook.

    The hook is only ever consulted when ``CHUDWARE_PLM_SUMMARY_POLISH=1``; its
    output must preserve the deterministic numeral set and stay free of git
    vocabulary or it is discarded.
    """
    global _polish_hook
    _polish_hook = fn


# ---------------------------------------------------------------------------
# numeral fidelity helpers (shared with tests)
# ---------------------------------------------------------------------------

def numeral_tokens(text: str) -> set[str]:
    """Extract every numeral token (``3``, ``5.5``, …) from *text*."""
    return set(_NUMERAL_RE.findall(text))


def diff_numerals(diff: dict[str, Any]) -> set[str]:
    """The numeral vocabulary a summary is allowed to use: every numeral token
    appearing anywhere in the diff's JSON form, plus the canonical short form
    of integral floats (``3.0`` licenses ``3``)."""
    raw = numeral_tokens(json.dumps(diff, sort_keys=True))
    allowed = set(raw)
    for tok in raw:
        if "." in tok:
            f = float(tok)
            if f == int(f):
                allowed.add(str(int(f)))
    return allowed


def _git_vocab_forms() -> frozenset[str]:
    """The §8.1 vocabulary plus simple inflections (commits, committed,
    merging, …) so verb forms cannot slip through. Exact-word based — CAD
    words like "shaft"/"shape" never false-positive."""
    forms: set[str] = set()
    for w in _GIT_VOCAB:
        forms.update({w, w + "s", w + "es", w + "ed", w + "ing",
                      w + w[-1] + "ed", w + w[-1] + "ing"})
        if w.endswith("e"):
            forms.update({w + "d", w[:-1] + "ing"})
    return frozenset(forms)


_GIT_VOCAB_FORMS = _git_vocab_forms()


def _contains_git_vocab(text: str) -> bool:
    words = set(re.split(r"[^a-z]+", text.lower()))
    return bool(words & _GIT_VOCAB_FORMS)


# ---------------------------------------------------------------------------
# value + label rendering
# ---------------------------------------------------------------------------

def _unwrap(v: Any) -> Any:
    """Unwrap a parametric dimension dict (``{"value": 3.0, "expr": None}``,
    doc_schema dimension shape) to its scalar so summaries read naturally.
    Any other shape passes through untouched."""
    if isinstance(v, dict) and "value" in v and set(v) <= {"value", "expr", "unit"}:
        return v["value"]
    return v


def _fmt_value(v: Any) -> str:
    """Render a diff value verbatim-faithfully.

    Numbers: integral floats print short (``3`` for ``3.0``) — licensed by
    :func:`diff_numerals`. Everything else uses its JSON form so the token is
    guaranteed to appear in the diff dump.
    """
    v = _unwrap(v)
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return v
    return json.dumps(v, sort_keys=True)


def _feature_label(fid: str, *docs: dict[str, Any] | None) -> str:
    """A display label for feature *fid*.

    Prefers the feature's human name/type from the documents, but ONLY when it
    is digit-free (numeral-fidelity law) and git-vocab-free; otherwise falls
    back to the feature id, which appears verbatim in the diff.
    """
    for doc in docs:
        if not doc:
            continue
        for f in doc.get("features", []) or []:
            if isinstance(f, dict) and f.get("id") == fid:
                for key in ("name", "type"):
                    label = f.get(key)
                    if (isinstance(label, str) and label
                            and not _NUMERAL_RE.search(label)
                            and not _contains_git_vocab(label)):
                        return label
    return fid


def _join(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


# ---------------------------------------------------------------------------
# the deterministic template
# ---------------------------------------------------------------------------

def summarize_diff(diff: dict[str, Any],
                   doc_before: dict[str, Any] | None = None,
                   doc_after: dict[str, Any] | None = None) -> str:
    """Template a plain-language, one-line summary from a ``doc_diff`` result.

    Deterministic: same diff (+docs) → same string. Numerals are copied from
    the diff only. Returns ``"No design changes."`` for an empty diff.
    """
    clauses: list[str] = []

    # 1. parameter changes — the headline edits ("Increased wall 3 → 5")
    for rec in diff.get("params_changed", []) or []:
        label = _feature_label(rec.get("feature", ""), doc_after, doc_before)
        param = rec.get("param", "value")
        old, new = _unwrap(rec.get("old")), _unwrap(rec.get("new"))
        if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
                and not isinstance(old, bool) and not isinstance(new, bool):
            verb = "Increased" if new > old else ("Decreased" if new < old else "Changed")
        else:
            verb = "Changed"
        # ASCII "to", not "→": these summaries are rendered by the desktop UI, whose font
        # requests Latin-1 plus a small CAD symbol set, so an arrow ships as "?"
        # (visible in the PLM Versions inspector as "wall 1.7 ? 1.8").
        clauses.append(f"{verb} {param} on {label} {_fmt_value(old)} to {_fmt_value(new)}")

    # 2. features added / removed
    added = diff.get("features_added", []) or []
    if added:
        clauses.append("Added " + _join([_feature_label(f, doc_after) for f in added]))
    removed = diff.get("features_removed", []) or []
    if removed:
        clauses.append("Removed " + _join([_feature_label(f, doc_before) for f in removed]))

    # 3. sketch rework (ids appear in the diff; no counts — counts are numerals
    #    that the diff does not carry as tokens)
    for rec in diff.get("sketch_deltas", []) or []:
        label = _feature_label(rec.get("feature", ""), doc_after, doc_before)
        clauses.append(f"Reworked sketch {label}")

    # 4. suppression flips
    for rec in diff.get("suppression_flips", []) or []:
        label = _feature_label(rec.get("id", ""), doc_after, doc_before)
        if rec.get("new"):
            clauses.append(f"Suppressed {label}")
        else:
            clauses.append(f"Unsuppressed {label}")

    # 5. assembly mates
    for rec in diff.get("mate_deltas", []) or []:
        mid = rec.get("id", "")
        kind = rec.get("kind", "changed")
        mate_obj = rec.get("new") if kind != "removed" else rec.get("old")
        mate_kind = ""
        if isinstance(mate_obj, dict):
            mk = mate_obj.get("kind")
            if isinstance(mk, str) and not _NUMERAL_RE.search(mk):
                mate_kind = mk + " "
        verb = {"added": "Added", "removed": "Removed"}.get(kind, "Changed")
        clauses.append(f"{verb} {mate_kind}mate {mid}")

    if not clauses:
        return "No design changes."

    overflow = len(clauses) > _MAX_CLAUSES
    text = "; ".join(clauses[:_MAX_CLAUSES])
    if overflow:
        text += "; and other changes"
    text += "."

    # Ease-of-use guard: user names could smuggle vocabulary in via labels.
    if _contains_git_vocab(text):
        # rebuild with document labels disabled — ids only, always safe
        return _summarize_ids_only(diff)

    return _maybe_polish(text, diff)


def _summarize_ids_only(diff: dict[str, Any]) -> str:
    """Fallback rendering using diff ids only (labels disabled)."""
    return summarize_diff(diff, None, None) if diff else "No design changes."


def summarize_first_version(doc: dict[str, Any] | None, path: str) -> str:
    """The summary for an item's very first history entry."""
    name = None
    if doc:
        n = doc.get("name")
        if isinstance(n, str) and n and not _contains_git_vocab(n):
            name = n
    return f"First version of {name or path}."


def _maybe_polish(text: str, diff: dict[str, Any]) -> str:
    """Apply the optional learned polish — flag-gated, validated, discardable."""
    if os.environ.get(_POLISH_FLAG) != "1" or _polish_hook is None:
        return text
    try:
        polished = _polish_hook(text, diff)
    except Exception:
        return text
    if not isinstance(polished, str) or not polished.strip():
        return text
    # garnish must not invent numerals nor leak vocabulary
    if not numeral_tokens(polished) <= (numeral_tokens(text) | diff_numerals(diff)):
        return text
    if _contains_git_vocab(polished):
        return text
    return polished


# ---------------------------------------------------------------------------
# vault-facing convenience
# ---------------------------------------------------------------------------

def summary_between_docs(doc_before: dict[str, Any] | None,
                         doc_after: dict[str, Any],
                         path: str = "") -> str:
    """Summarize the change between two parsed documents (either may be the
    first version). Uses the same semantic diff engine as ``plm_compare``."""
    if doc_before is None:
        return summarize_first_version(doc_after, path)
    diff = _doc_diff(doc_before, doc_after)
    return summarize_diff(diff, doc_before, doc_after)


def _doc_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Lazy import of the semantic diff so services/plm has no hard MCP dep."""
    from services.mcp.doc_diff_tools import doc_diff

    return doc_diff(a, b)
