"""Shared identity-aware relevance for local and live parts search."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_NEMA = re.compile(r"(?<![a-z0-9])nema\s*[- ]?\s*(\d+)(?![a-z0-9])", re.I)
_CODE = re.compile(
    r"(?<![a-z0-9])(?:[a-z]*\d+[a-z0-9]*(?:[-/][a-z0-9]+)+|"
    r"[a-z]+\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?|\d+[a-z]*)(?![a-z0-9])",
    re.I,
)
_STOP_WORDS = {"a", "an", "and", "the", "for", "of", "with", "in", "mm", "inch", "inches"}
_ACCESSORY_WORDS = {"adapter", "adaptor", "lubricant", "grease", "mount", "mounts",
                    "holder", "housing", "bracket", "kit", "clip", "arm", "arms",
                    "horn", "pulley", "finger", "claw", "cover", "support", "gear",
                    "gears", "pan", "tilt", "turret", "turet", "base", "tool",
                    "pusher", "helper", "removal", "applicator", "spool", "stand", "mod",
                    "label", "labels", "spacer", "gearbox"}


@lru_cache(maxsize=512)
def _query_parts(query: str) -> tuple[tuple[re.Pattern, ...], tuple[str, ...]]:
    """Compile identifiers separately from descriptive words.

    A NEMA frame is a single identity, not a loose match on an unrelated '17'.
    Thread lengths may be written M3x10 or M3 x 10. Bearing shield suffixes are
    accepted, but another numeric size (6080, 1608, M30) is never a match.
    """
    remaining = query.casefold()
    patterns = []
    for match in _NEMA.finditer(remaining):
        patterns.append(re.compile(rf"(?<![a-z0-9])nema\s*[- ]?\s*{match.group(1)}(?![a-z0-9])", re.I))
    remaining = _NEMA.sub(" ", remaining)
    for match in _CODE.finditer(remaining):
        code = re.sub(r"\s+", "", match.group()).replace("×", "x")
        if "x" in code and re.fullmatch(r"m\d+(?:\.\d+)?x\d+(?:\.\d+)?", code):
            thread, length = code.split("x", 1)
            body = rf"{re.escape(thread)}\s*[x×]\s*{re.escape(length)}"
            suffix = r"(?![a-z0-9]|\.\d)"
        else:
            body = re.escape(code)
            if re.fullmatch(r"m\d+(?:\.\d+)?", code):
                suffix = r"(?!\.\d)(?=$|[^a-z0-9]|x\s*\d)"
            elif code.isdigit():
                suffix = r"(?![./]\d)(?=$|[^a-z0-9]|(?:zz|z|2rs|rs|2rsh|2z|llu|llb)(?![a-z0-9]))"
            else:
                suffix = r"(?![a-z0-9])"
        patterns.append(re.compile(rf"(?<![a-z0-9]){body}{suffix}", re.I))
    remaining = _CODE.sub(" ", remaining)
    words = [word for word in _WORD.findall(remaining) if word not in _STOP_WORDS]
    return tuple(patterns), tuple(words)


def _identity_text(rec: dict) -> str:
    params = rec.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return " ".join(str(value or "") for value in (
        rec.get("title"), rec.get("designation"), rec.get("standard"),
        params.get("sku"), params.get("mpn"), params.get("part_number"),
        params.get("model_file"), *(rec.get("aliases") or []),
        rec.get("description") or params.get("description"),
    )).casefold()


def identifiers_match(text: str, query: str) -> bool:
    """True only if the query contains identifiers and all match as tokens."""
    identifiers, _ = _query_parts(query)
    return bool(identifiers) and all(pattern.search(text) for pattern in identifiers)


def has_identifiers(query: str) -> bool:
    return bool(_query_parts(query)[0])


def _word_matches(word: str, text: str) -> bool:
    # Most catalog titles use the plural form of the user's component name.
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?:s|es)?(?![a-z0-9])", text))


def descriptive_relevant(rec: dict, query: str) -> bool:
    """Keep neighboring local sizes only when they still describe the part."""
    identifiers, words = _query_parts(query)
    if not identifiers or not words:
        return True
    text = _identity_text(rec)
    return sum(_word_matches(word, text) for word in words) >= max(1, (len(words) + 1) // 2)


def live_relevant(rec: dict, query: str) -> bool:
    """Require all requested identity tokens and meaningful descriptive coverage.

    Unidentified live results are never substituted for an explicitly requested
    designation; local neighboring sizes may still fill unused result slots.
    """
    if not query.strip():
        return True
    identifiers, words = _query_parts(query)
    text = _identity_text(rec)
    if not all(pattern.search(text) for pattern in identifiers):
        return False
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    site = str(params.get("site") or "")
    if not site and str(rec.get("part_id") or "").startswith("catalog:"):
        site = str(rec["part_id"])[len("catalog:"):].split("::", 1)[0]
    if identifiers and words and site in {"grabcad", "printables", "makerworld", "3dcontentcentral"}:
        title_words = set(_WORD.findall(str(rec.get("title") or "").casefold()))
        if (_ACCESSORY_WORDS & title_words) - set(_WORD.findall(query.casefold())):
            return False
    # Community models often describe an assembly *using* the searched part,
    # not the part itself (e.g. a fixture \"with 608 bearing\"). The identifier
    # must identify the subject, unless the user explicitly asked for that
    # relationship.
    if identifiers and not ({"for", "with", "using"} & set(_WORD.findall(query.casefold()))):
        title = str(rec.get("title") or "").casefold()
        positions = [match.start() for pattern in identifiers if (match := pattern.search(title))]
        if positions and re.search(r"\b(?:for|with|using)\b", title[:min(positions)]):
            return False
    if not words:
        return bool(identifiers)
    matched = sum(_word_matches(word, text) for word in words)
    return matched >= max(1, (len(words) + 1) // 2)


def score_record(rec: dict, query: str) -> int:
    """Rank exact identities ahead of neighboring sizes, preserving GUI scoring."""
    terms = _WORD.findall(query.casefold())
    if not terms:
        return 0
    name = str(rec.get("title") or "").casefold()
    designation = str(rec.get("designation") or "").casefold()
    text = _identity_text(rec)
    matched = sum(_word_matches(term, text) for term in terms)
    if query.casefold().strip() == designation:
        return 100
    if not matched:
        # NEMA17 and NEMA 17 are equivalent even when tokenization differs.
        identifiers, _ = _query_parts(query)
        if not identifiers or not all(pattern.search(text) for pattern in identifiers):
            return 0
    identity_match = any(_word_matches(term, designation) for term in terms)
    score = min(95, round(70 * matched / len(terms)) +
                (15 if query.casefold().strip() in name else 0) +
                (20 if identity_match else 0))
    # An alias can make a different part discoverable, but the displayed
    # identity is stronger evidence of the requested component type.
    primary = f"{name} {designation}"
    alias_only = sum(_word_matches(term, text) and not _word_matches(term, primary)
                     for term in terms)
    score = max(0, score - min(25, 5 * alias_only))
    if rec.get("domain") == "catalog" and rec.get("source") == "live":
        title_words = set(_WORD.findall(name))
        filename = str((rec.get("params") or {}).get("model_file") or "").casefold()
        file_words = set(_WORD.findall(filename))
        unexpected_accessory = bool((_ACCESSORY_WORDS & (title_words | file_words)) - set(terms))
        if unexpected_accessory:
            score = max(0, score - 20)
        model_codes = [term for term in terms if len(term) >= 4 and any(c.isdigit() for c in term)]
        if filename and model_codes:
            if not any(code in filename for code in model_codes):
                score = max(0, score - 25)
            elif not unexpected_accessory:
                score = min(95, score + 15)
                suffix = Path(filename).suffix
                score = min(99, score + (4 if suffix in {".step", ".stp", ".iges", ".igs"}
                                         else 2 if suffix == ".3mf" else 0))
        if (model_codes and re.search(r"\b(?:mg90s|sg90|mg995|mg996r|ds3218)\b", query.casefold())
                and not ({"servo", "motor"} & title_words)):
            score = max(0, score - 20)
    identifiers, _ = _query_parts(query)
    if identifiers:
        if all(pattern.search(text) for pattern in identifiers):
            params = rec.get("params") or {}
            if rec.get("source") == "live" and isinstance(params, dict):
                part_number = " ".join(str(params.get(key) or "")
                                       for key in ("sku", "mpn", "part_number"))
                if all(pattern.search(part_number) for pattern in identifiers):
                    score = max(score, 90)
            score = max(50, score)
        else:
            score = min(49, score)
    return score
