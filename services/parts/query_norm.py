"""Query normalization and synonym expansion for parts search.

Pure, dependency-free, never raises. Two entry points:

- ``normalize_query(q)`` — lowercase, collapse whitespace, unify dashes/x/×,
  strip stray punctuation, while PRESERVING designation-significant structure
  (``2-014``, ``6061-t6``, ``m8x1.25``, ``608zz``, ``1/4-20``, ``nema17``).
- ``expand_terms(q)`` — one synonym group per meaningful term. Vendors write
  the same thing glued (``nema17``) or split (``nema 17``), abbreviated
  (``shcs``) or spelled out (``socket head cap screw``), British or American
  (``aluminium``/``aluminum``) — every group carries all known spellings so a
  downstream FTS OR-query actually finds the part. Group[0] is always the
  canonical term; stopwords produce no group at all.

Both functions are used by ``services/parts/index.py`` (ranking / term
coverage) and ``services/parts/hub.py`` (recall gating) — keep the return
shapes exactly as documented above; callers depend on them.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Bounds — a pathological query must never blow up downstream FTS building.
# ---------------------------------------------------------------------------

_MAX_INPUT_CHARS = 2000
_MAX_GROUPS = 24

_STOPWORDS = frozenset({
    "a", "an", "the", "for", "with", "of", "and", "to", "in", "my", "some",
    "need", "want", "please", "part", "parts", "size",
})

# ---------------------------------------------------------------------------
# normalize_query
# ---------------------------------------------------------------------------

# Unicode dashes/minus variants -> ascii hyphen.
_DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DASH_TABLE = str.maketrans({c: "-" for c in _DASH_CHARS})

# Characters we keep because they're designation-significant across common
# CAD/mechanical part numbers: alnum, whitespace, and -./_# (fractions, dash
# ranges, decimal dims, o-ring dash codes, thread pitch dots).
_KEEP_PUNCT = "-./_#"
_STRIP_RE = re.compile(r"[^0-9a-z\s" + re.escape(_KEEP_PUNCT) + r"]+")
_WS_RE = re.compile(r"\s+")
# Standalone "x"/"×" used as a dimension separator between two numeric-ish
# tokens, e.g. "8 x 1.25" / "8×1.25" / "8 X 1.25" -> "8x1.25".
_DIM_X_RE = re.compile(r"(?<=[0-9])\s*[×xX]\s*(?=[0-9])")
_WS_AROUND_X_RE = re.compile(r"\s+x\s+")


def normalize_query(q: str) -> str:
    """Lowercase/clean a raw search string while preserving part-number shape."""
    try:
        if q is None:
            return ""
        s = str(q)[:_MAX_INPUT_CHARS]
        s = s.translate(_DASH_TABLE)
        s = s.replace("\u00d7", "x")  # multiplication sign
        s = s.lower()
        # Unify "8 x 1.25" style spacing before stripping stray punctuation.
        s = _DIM_X_RE.sub("x", s)
        s = _WS_AROUND_X_RE.sub("x", s)
        s = _STRIP_RE.sub(" ", s)
        s = _WS_RE.sub(" ", s).strip()
        return s
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# expand_terms
# ---------------------------------------------------------------------------

# Bidirectional synonym/abbreviation groups, keyed by the exact word(s) that
# should TRIGGER the group. A generic member word (e.g. "screw", "head",
# "diameter") can appear inside more than one group's *display* list without
# being a trigger for all of them — e.g. "screw" alone must resolve to the
# screw/bolt group, not silently swallow into the SHCS abbreviation group.
# Multi-word phrases are folded to their abbreviation before tokenizing (see
# _PHRASE_TO_ABBREV), so a bare "head"/"cap"/"diameter" is simply not a
# trigger anywhere — it only ever appears as a display-only member.
_TRIGGER_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("shcs",), ("shcs", "socket", "head", "cap", "screw")),
    (("bhcs",), ("bhcs", "button", "head", "cap", "screw")),
    (("fhcs",), ("fhcs", "flat", "head", "cap", "screw", "countersunk")),
    (("hhcs",), ("hhcs", "hex", "head", "cap", "screw")),
    (("oring", "o-ring", "orings"), ("oring", "o-ring", "orings")),
    (("id", "bore"), ("id", "inner", "diameter", "bore")),
    (("od",), ("od", "outer", "diameter")),
    (("thk", "thickness"), ("thk", "thickness")),
    (("aluminum", "al", "aluminium"), ("aluminum", "al", "aluminium")),
    (("stainless", "ss"), ("stainless", "ss")),
    (("cs", "carbon", "steel"), ("cs", "carbon", "steel")),
    (("bearing", "brg"), ("bearing", "brg")),
    (("qty", "quantity"), ("qty", "quantity")),
    (("screw", "bolt"), ("screw", "bolt")),
    (("stepper", "motor"), ("stepper", "step", "motor")),
    (("pulley", "sheave"), ("pulley", "sheave")),
    (("seal", "gasket"), ("seal", "gasket")),
    (("extrusion", "profile"), ("extrusion", "profile")),
    (("tube", "tubing"), ("tube", "tubing")),
    (("rod", "shaft", "bar"), ("rod", "shaft", "bar")),
    (("sheet", "plate"), ("sheet", "plate")),
    (("gauge", "gage"), ("gauge", "gage")),
    (("fiber", "fibre"), ("fiber", "fibre")),
)

# term -> canonical group tuple. Built once at import time. First registration
# for a trigger word wins (there is no legitimate overlap given the table
# above, but stay defensive rather than silently reordering).
_SYNONYM_LOOKUP: dict[str, tuple[str, ...]] = {}
for _triggers, _group in _TRIGGER_GROUPS:
    for _trigger in _triggers:
        _SYNONYM_LOOKUP.setdefault(_trigger, _group)

# Compound phrases that collapse to one abbreviation before tokenizing, so
# "socket head cap screw" produces one group instead of scattering "head"/
# "cap" as meaningless bare tokens.
_PHRASE_TO_ABBREV = {
    "socket head cap screw": "shcs",
    "button head cap screw": "bhcs",
    "flat head cap screw": "fhcs",
    "hex head cap screw": "hhcs",
    "step motor": "stepper",
    "inner diameter": "id",
    "outer diameter": "od",
    "carbon steel": "cs",
}

# Words that only ever appear as display-only members of a phrase group
# above (never a lookup trigger) and are meaningless alone — drop them as
# bare tokens instead of emitting a useless single-word group.
_BARE_NOISE_WORDS = frozenset({"head", "cap", "countersunk", "inner", "outer", "diameter"})

# alpha<->digit glue split, e.g. "nema17" -> ("nema", "17"); "608zz" -> ("608", "zz").
_GLUE_RE = re.compile(r"^([a-z]+)(\d+[a-z0-9]*)$|^(\d+)([a-z]+)$")

_TOKEN_RE = re.compile(r"[0-9a-z][0-9a-z./_#-]*")


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _split_glued(token: str) -> list[str]:
    """Split a glued alphanumeric into its parts, e.g. 'nema17' -> ['nema', '17']."""
    m = _GLUE_RE.match(token)
    if not m:
        return []
    if m.group(1) is not None:
        return [m.group(1), m.group(2)]
    return [m.group(3), m.group(4)]


def _group_for_term(term: str) -> list[str]:
    """Build the synonym group for one already-normalized token, canonical first."""
    variants = [term]

    glued_parts = _split_glued(term)
    variants.extend(glued_parts)

    # Resolve synonyms for the term itself and for its glued parts.
    resolved: list[str] = []
    for v in (term, *glued_parts):
        syn_group = _SYNONYM_LOOKUP.get(v)
        if syn_group:
            resolved.extend(syn_group)

    canonical = resolved[0] if resolved else term
    ordered = [canonical, *variants, *resolved]
    return _dedupe(ordered)


def expand_terms(q: str) -> list[list[str]]:
    """Split a normalized query into synonym groups, canonical term at index 0."""
    try:
        norm = normalize_query(q)
        if not norm:
            return []

        # Fold known multi-word phrases into their abbreviation before tokenizing,
        # so "socket head cap screw" collapses to one group instead of four.
        working = norm
        for phrase, abbrev in _PHRASE_TO_ABBREV.items():
            working = working.replace(phrase, abbrev)

        raw_tokens = _TOKEN_RE.findall(working)
        groups: list[list[str]] = []
        for tok in raw_tokens:
            if len(groups) >= _MAX_GROUPS:
                break
            if tok in _STOPWORDS or tok in _BARE_NOISE_WORDS:
                continue
            groups.append(_group_for_term(tok))
        return groups[:_MAX_GROUPS]
    except Exception:
        return []
