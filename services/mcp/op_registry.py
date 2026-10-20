"""Document op vocabulary — Python twin of ``core/doc/op_registry.{h,cpp}``.

Live dispatch is C++ (``core/doc/session.cpp``). This module keeps the closed
list + suggestion/error text in lockstep for:

  * offline unit tests
  * MCP / agent surfaces that prefer a pure-Python import
  * parity checks against ``doc_list_ops`` when libchudware is loaded

If you add a handler in session.cpp, update BOTH ``core/doc/op_registry.cpp``
and ``DOC_OP_KINDS`` here. ``test_op_vocab.py`` fails when they diverge from
the C++ registry (via ``doc_list_ops``) or from each other.
"""
from __future__ import annotations

from typing import Iterable

# SINGLE SOURCE (Python twin). Mirror of kDocOpKinds in core/doc/op_registry.cpp.
DOC_OP_KINDS: tuple[str, ...] = (
    "add_feature",
    "edit_feature",
    "delete_feature",
    "reorder_feature",
    "set_suppressed",
    "set_folder",
    "rename",
    "set_units",
    "set_material",
    "set_appearance",
    "set_rollback",
    "eq_set",
    "eq_delete",
    "sketch_add_entity",
    "sketch_add_constraint",
    "sketch_edit_entity",
    "sketch_delete_entity",
    "sketch_delete_constraint",
    "sketch_set_dim",
    "sketch_fully_define",
    "sketch_toggle_construction",
    "sketch_convert",
    "sketch_break_external_ref",
    "sketch_trim",
    "sketch_extend",
    "sketch_offset",
    "sketch_fillet",
    "sketch_chamfer",
    "sketch_mirror_entities",
    "sketch_pattern_entities",
    "sketch_move",
    "sketch_rotate",
    "asm_insert_component",
    "asm_edit_component",
    "asm_delete_component",
    "asm_add_mate",
    "asm_edit_mate",
    "asm_delete_mate",
    "asm_set_explode",
    "copy_feature",
    "paste_feature",
    "paste_feature_as_derived",
    "sketch_paste_entities",
)

# Common feature types under add_feature (subset of FEATURE_TYPES in validate.cpp).
# Full list is available via doc_list_ops / feature_type_names().
COMMON_FEATURE_TYPES: tuple[str, ...] = (
    "box", "cylinder", "sphere", "cone", "torus",
    "sketch", "extrude", "revolve", "loft", "sweep", "helix",
    "fillet", "chamfer", "shell", "draft", "hole", "bolt_circle",
    "linear_pattern", "circular_pattern", "mirror", "transform", "boolean",
    "rib", "split", "thread", "text", "import", "script",
    "plane", "axis", "point", "csys", "sketch3d",
)


def _family(name: str) -> str:
    i = name.find("_")
    return name[:i] if i > 0 else name


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _score_match(q: str, cand: str) -> int:
    q = (q or "").lower()
    cand_l = cand.lower()
    if not q:
        return 0
    if cand_l == q:
        return 100000
    if cand_l.startswith(q):
        return 50000 + len(q) * 10
    if q.startswith(cand_l):
        return 45000 + len(cand_l) * 10
    qf, cf = _family(q), _family(cand_l)
    if len(qf) >= 2 and qf == cf:
        qrest = q[len(qf) + 1:] if len(q) > len(qf) + 1 else ""
        crest = cand_l[len(cf) + 1:] if len(cand_l) > len(cf) + 1 else ""
        bonus = 0
        if qrest and crest.startswith(qrest):
            bonus = 2000 + len(qrest) * 10
        elif qrest:
            bonus = 1000 - _levenshtein(qrest, crest) * 20
        return 30000 + bonus
    if q in cand_l:
        return 20000 + len(q)
    if cand_l in q:
        return 15000 + len(cand_l)
    # Final resort: a plain typo. A short query is within a handful of edits of almost
    # every short op name ("cut" -> "eq_set" is 6), so an edit distance that large is
    # NOISE, not a suggestion — it is what made an unknown op report
    # "closest: eq_set, rename, set_units". Only a distance small relative to the words
    # being compared (a third of the shorter one, at least 1) counts as a relation;
    # anything else scores 0 and suggest_doc_ops drops it entirely.
    lev = _levenshtein(q, cand_l)
    if lev > max(1, min(len(q), len(cand_l)) // 3):
        return 0
    return 10000 - lev * 100


# The words a model reaches for when it means "remove material". None of them is an op;
# every one of them is add_feature + params.mode:"cut".
_REMOVAL_WORDS = frozenset({
    "cut", "pocket", "subtract", "remove", "difference", "hollow_out", "mill",
    "carve", "excavate", "recess", "cutout", "cut_out", "notch",
})

# Live "suppress"->set_suppressed (dbg-fix5-1: {"op":"suppress","target":"f3"} got
# "closest: set_suppressed, ..." only because "suppress" is a SUBSTRING of
# "set_suppressed" — plain scoring luck. Its natural sibling "unsuppress" (and the
# analogous "change_units" for set_units) share NO substring with the canonical name,
# so _score_match returns 0 for them and the model falls into the generic "nothing is
# close" vocabulary dump instead of a precise pointer. Each entry here maps to exactly
# ONE real op — there is no other doc op an "unsuppress"/"change_units" call could mean.
_OP_SYNONYMS: dict[str, str] = {
    "unsuppress": "set_suppressed",
    "un_suppress": "set_suppressed",
    "change_units": "set_units",
    "convert_units": "set_units",
}


def doc_op_kinds() -> list[str]:
    return list(DOC_OP_KINDS)


def suggest_doc_ops(unknown: str, n: int = 5,
                    feature_types: Iterable[str] | None = None) -> list[str]:
    """Closest N op kinds. Feature-type confusion pins ``add_feature`` first.

    An exact hit in ``_OP_SYNONYMS`` is pinned first — those names have no shared
    substring with their real op, so plain scoring would otherwise miss them.
    """
    q = (unknown or "").lower()
    synonym = _OP_SYNONYMS.get(q)
    scored = [(_score_match(q, k), k) for k in DOC_OP_KINDS]
    ftypes = list(feature_types) if feature_types is not None else list(COMMON_FEATURE_TYPES)
    is_ft = any(
        ft.lower() == q or _score_match(q, ft.lower()) >= 20000
        for ft in ftypes
    )
    if is_ft:
        scored = [
            (200000 if k == "add_feature" else sc, k) for sc, k in scored
        ]
    scored.sort(key=lambda p: (-p[0], p[1]))
    # A zero score is "no genuine relation" (see _score_match). A misleading list is
    # worse than none: it sent a model chasing eq_set/rename after writing op:"cut".
    rest = [k for sc, k in scored[: max(0, n)] if sc > 0]
    if synonym is None:
        return rest
    return [synonym] + [k for k in rest if k != synonym][: max(0, n - 1)]


def unknown_doc_op_message(index: int, kind: str,
                           feature_types: Iterable[str] | None = None) -> str:
    ftypes = list(feature_types) if feature_types is not None else list(COMMON_FEATURE_TYPES)
    q = (kind or "").lower()
    matched = next((ft for ft in ftypes if ft.lower() == q), None)
    msg = f"op[{index}]: unknown op '{kind}'"
    if matched:
        msg += (
            f". '{matched}' is a feature TYPE, not an op — use "
            f'{{"op":"add_feature","feature":{{"type":"{matched}",...}}}}'
        )
    closest = suggest_doc_ops(kind, 5, feature_types=ftypes)
    if closest:
        msg += ". closest: " + ", ".join(closest)
    elif not matched:
        # Nothing in the vocabulary is even close, so the model is not making a typo —
        # it is writing in the wrong vocabulary. Teach the shape instead of guessing.
        msg += (
            ". Ops are add_feature, edit_feature, delete_feature, set_material, rename, "
            "eq_set, sketch_* … — geometry is never an op name: a shape is a feature TYPE "
            'under add_feature ({"op":"add_feature","feature":{"type":"extrude",…}}), and '
            'a material-REMOVING operation is that same add_feature with params.mode:"cut"'
        )
        if q in _REMOVAL_WORDS:
            msg += (
                f' — so "{kind}" is {{"op":"add_feature","feature":{{"type":"extrude",'
                f'"name":"Pocket","refs":{{"profile":[…]}},"params":{{"mode":"cut",'
                f'"depth":10}}}}}}'
            )
    msg += "; see cad_guide()"
    return msg


def doc_ops_cheatsheet_snippet() -> str:
    kinds = DOC_OP_KINDS[:25]
    head = ", ".join(kinds) + (", …" if len(DOC_OP_KINDS) > 25 else "")
    common = ", ".join((
        "box", "cylinder", "sphere", "sketch", "extrude", "revolve",
        "fillet", "chamfer", "hole", "mirror", "linear_pattern", "circular_pattern",
    ))
    return (
        "[op vocabulary] Document ops (field op=...):\n  "
        + head
        + "\nPrimitives/solids are feature TYPES under add_feature, NOT top-level ops. "
        "Common types: "
        + common
        + '.\nExample: {"op":"add_feature","feature":{"type":"box","name":"B",'
        '"params":{"length":{"value":40,"expr":null},'
        '"width":{"value":30,"expr":null},'
        '"height":{"value":20,"expr":null}}}}\n'
        "Call cad_guide() for the full vocabulary + worked examples."
    )
