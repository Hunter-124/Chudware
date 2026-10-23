"""Corruption-corpus generator for the regen-fix bench (IP-18).

Mutates known-good `aicad.part/1` documents into ones whose rebuild fails, across the five
mutation classes the bench targets:

    dangling_ref        a selector / profile / pattern points at a feature id that is gone
    param_out_of_range  a numeric parameter is driven to an impossible value
    suppressed_parent   a parent feature is suppressed while children still reference it
    circular_equation   two global equations reference each other (a cycle is a doc error)
    bad_selector        a selector's `kind` is corrupted to an invalid token

This is a JSON→JSON transform: it needs **no C++ core** to produce the corpus, so the whole
≥500-doc corpus is reproducible from a git-committed seed + a fixed RNG seed on any OS.
The core is only needed later to *verify* a proposed fix rebuilds
(the regen_fix scorer's live path).

Each example records ground truth including `det_fixable` — whether the deterministic
kill-switch baseline can repair that class. The classes it cannot (`dangling_ref`,
`bad_selector`) are the measured residual a model must beat.

Held-out split: the STRUCTURE key is the seed variant, so every mutation of one variant
stays on the same side of a train/test split (hold out structures, not samples).
"""
from __future__ import annotations

import copy
import json
import os
import random
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from ..dataset import DatasetSpec

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
SEED_FIXTURE = os.path.join(_REPO_ROOT, "contracts", "fixtures", "part_bracket.json")

# Mutation classes and whether the deterministic baseline fixer can repair each CLEANLY
# (rebuild passes AND design intent preserved). The three residual classes are what a
# trained IP-18 model must beat.
DET_FIXABLE = {
    "dangling_ref": False,       # can't invent the correct target -> residual
    "param_out_of_range": True,  # clamp the impossible feature-local value back to sane
    "suppressed_parent": True,   # un-suppress the parent
    "circular_equation": False,  # can break the cycle but only by GUESSING an intent value
                                 # -> a clean kill-switch won't drift intent -> residual
    "bad_selector": False,       # can't infer the correct kind -> residual
}
MUTATION_CLASSES = tuple(DET_FIXABLE.keys())


@dataclass
class MutationLabel:
    example_id: str
    structure: str          # seed-variant key (held-out split unit)
    mutation_class: str
    target: str             # feature id / global name touched
    det_fixable: bool
    fixable: bool           # is it repairable in principle (a model could)
    intent_dims: dict[str, Any]  # dimensions that must NOT drift after a fix

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ---- seed + variants --------------------------------------------------------

def load_seed(path: str = SEED_FIXTURE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _intent_dims(doc: dict) -> dict[str, Any]:
    """The equation globals are the design-intent dimensions; a fix must preserve them."""
    return {g["name"]: g["expr"] for g in doc.get("equations", {}).get("globals", [])}


def parametric_variant(seed: dict, rng: random.Random, idx: int) -> dict:
    """A distinct known-good doc: scale the dimension globals by a deterministic factor."""
    doc = copy.deepcopy(seed)
    doc["id"] = f"{seed.get('id', 'seed')}-v{idx}"
    doc["name"] = f"{seed.get('name', 'part')}_v{idx}"
    for g in doc.get("equations", {}).get("globals", []):
        try:
            base = float(g["expr"])
        except (TypeError, ValueError):
            continue
        factor = rng.choice([0.5, 0.75, 1.25, 1.5, 2.0])
        g["expr"] = str(round(base * factor, 3))
    return doc


# ---- feature/selector walkers ----------------------------------------------

def _features(doc: dict) -> list[dict]:
    return doc.get("features", [])


def _child_parents(doc: dict) -> dict[str, list[str]]:
    """feature_id -> list of feature ids that reference it via a selector `owner_feature`
    or a profile `sketch`. Used to pick a parent whose suppression breaks children."""
    parents: dict[str, list[str]] = {}
    for feat in _features(doc):
        for owner in _referenced_feature_ids(feat):
            parents.setdefault(owner, []).append(feat["id"])
    return parents


def _referenced_feature_ids(feat: dict) -> set[str]:
    """Feature ids this feature depends on: selector `owner_feature`/`sketch` keys, plus
    a `pattern.features` list of plain feature ids."""
    ids: set[str] = set()
    refs = feat.get("refs", {})

    def walk(node: Any):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("owner_feature", "sketch") and isinstance(v, str):
                    ids.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    feats_list = refs.get("features")
    if isinstance(feats_list, list):
        ids.update(x for x in feats_list if isinstance(x, str))
    walk({k: v for k, v in refs.items() if k != "features"})
    return ids


def _all_selectors(feat: dict) -> Iterable[dict]:
    """Yield selector dicts (have a 'kind') inside a feature's refs."""
    def walk(node: Any):
        if isinstance(node, dict):
            if "kind" in node and isinstance(node.get("kind"), str):
                yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for it in node:
                yield from walk(it)
    yield from walk(feat.get("refs", {}))


def _numeric_params(feat: dict) -> list[dict]:
    """Yield {'value': float, 'expr': ...} param dicts inside a feature's params."""
    out: list[dict] = []

    def walk(node: Any):
        if isinstance(node, dict):
            if "value" in node and isinstance(node.get("value"), (int, float)) and "expr" in node:
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)
    walk(feat.get("params", {}))
    return out


# ---- the five mutators ------------------------------------------------------
# Each returns the touched target id, or None if not applicable to this doc.

def mut_dangling_ref(doc: dict, rng: random.Random) -> Optional[str]:
    cands = [f for f in _features(doc) if _referenced_feature_ids(f)]
    if not cands:
        return None
    feat = rng.choice(cands)
    # repoint one owner_feature/sketch to a ghost id
    for sel in list(_all_selectors(feat)):
        if isinstance(sel.get("owner_feature"), str):
            sel["owner_feature"] = "f_ghost"
            return feat["id"]
    refs = feat.get("refs", {})
    if isinstance(refs.get("features"), list) and refs["features"]:
        refs["features"][0] = "f_ghost"
        return feat["id"]
    for sel in list(_all_selectors(feat)):
        if isinstance(sel.get("sketch"), str):
            sel["sketch"] = "f_ghost"
            return feat["id"]
    return None


def mut_param_out_of_range(doc: dict, rng: random.Random) -> Optional[str]:
    for feat in rng.sample(_features(doc), len(_features(doc))):
        params = _numeric_params(feat)
        if params:
            p = rng.choice(params)
            p["expr"] = None                 # make the literal value authoritative
            p["value"] = -abs(p["value"]) - 1e6  # impossible negative magnitude
            return feat["id"]
    return None


def mut_suppressed_parent(doc: dict, rng: random.Random) -> Optional[str]:
    parents = _child_parents(doc)
    if not parents:
        return None
    pid = rng.choice(list(parents.keys()))
    for feat in _features(doc):
        if feat["id"] == pid:
            feat["suppressed"] = True
            return pid
    return None


def mut_circular_equation(doc: dict, rng: random.Random) -> Optional[str]:
    globals_ = doc.get("equations", {}).get("globals", [])
    if len(globals_) < 2:
        return None
    a, b = rng.sample(globals_, 2)
    a["expr"] = b["name"]
    b["expr"] = a["name"]
    return a["name"]


def mut_bad_selector(doc: dict, rng: random.Random) -> Optional[str]:
    for feat in rng.sample(_features(doc), len(_features(doc))):
        sels = [s for s in _all_selectors(feat) if "kind" in s]
        if sels:
            rng.choice(sels)["kind"] = "not_a_kind"
            return feat["id"]
    return None


MUTATORS: dict[str, Callable[[dict, random.Random], Optional[str]]] = {
    "dangling_ref": mut_dangling_ref,
    "param_out_of_range": mut_param_out_of_range,
    "suppressed_parent": mut_suppressed_parent,
    "circular_equation": mut_circular_equation,
    "bad_selector": mut_bad_selector,
}


def mutate(good_doc: dict, mutation_class: str, rng: random.Random) -> tuple[Optional[dict], Optional[str]]:
    """Return (corrupted_doc, target) or (None, None) if the class doesn't apply."""
    doc = copy.deepcopy(good_doc)
    target = MUTATORS[mutation_class](doc, rng)
    return (doc, target) if target is not None else (None, None)


# ---- corpus builder ---------------------------------------------------------

def build_corpus(out_dir: str, *, count: int = 500, seed: int = 1337,
                 n_variants: int = 12, seed_fixture: str = SEED_FIXTURE) -> DatasetSpec:
    """Deterministically build `count` corrupted docs into `out_dir/<example_id>/document.json`,
    write `labels.jsonl`, and freeze a DatasetSpec (spec.json). Reproducible from (seed,
    seed_fixture, count, n_variants) on any OS — no core needed.
    """
    os.makedirs(out_dir, exist_ok=True)
    base = load_seed(seed_fixture)
    rng = random.Random(seed)
    variants = [parametric_variant(base, rng, i) for i in range(n_variants)]

    labels: list[MutationLabel] = []
    made = 0
    attempt = 0
    while made < count:
        variant = variants[attempt % n_variants]
        mclass = MUTATION_CLASSES[attempt % len(MUTATION_CLASSES)]
        attempt += 1
        corrupted, target = mutate(variant, mclass, random.Random(seed + attempt))
        if corrupted is None:
            continue
        ex_id = f"{mclass}_{made:04d}"
        ex_dir = os.path.join(out_dir, ex_id)
        os.makedirs(ex_dir, exist_ok=True)
        with open(os.path.join(ex_dir, "document.json"), "w", encoding="utf-8") as f:
            json.dump({"revision": 0, "tree": corrupted, "owner_map": {}}, f,
                      indent=2, sort_keys=True)
        # The pre-mutation known-good doc — the reference the LIVE binary verifier compares
        # a proposed fix against (rebuild passes AND compare_parts ≈100% vs this). JSON→JSON,
        # still no core needed to BUILD it; the core is only used later to compare.
        with open(os.path.join(ex_dir, "good.json"), "w", encoding="utf-8") as f:
            json.dump({"revision": 0, "tree": variant, "owner_map": {}}, f,
                      indent=2, sort_keys=True)
        labels.append(MutationLabel(
            example_id=ex_id, structure=variant["id"], mutation_class=mclass,
            target=target, det_fixable=DET_FIXABLE[mclass], fixable=True,
            intent_dims=_intent_dims(variant),
        ))
        made += 1

    with open(os.path.join(out_dir, "labels.jsonl"), "w", encoding="utf-8") as f:
        for lab in labels:
            f.write(json.dumps(lab.to_dict(), sort_keys=True) + "\n")

    structures = sorted({lab.structure for lab in labels})
    spec = DatasetSpec.freeze("regen_fix_corpus", out_dir, split="all",
                              dvc_ref="corpus/regen_fix/v1", n_examples=made,
                              structures=structures)
    spec.write(os.path.join(out_dir, "spec.json"))
    return spec


def load_good(corpus_dir: str, example_id: str) -> Optional[dict]:
    """The pre-mutation known-good doc for one example (the live verifier's reference), or
    None for corpora built before good.json was emitted."""
    path = os.path.join(corpus_dir, example_id, "good.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_labels(corpus_dir: str) -> list[dict]:
    out = []
    with open(os.path.join(corpus_dir, "labels.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
