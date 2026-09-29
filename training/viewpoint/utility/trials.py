"""Trials: the view SETS the utility model learns to rank, and the observer labels on them.

Per part the candidate cameras and their measured metrics are computed ONCE (per target in
target mode) exactly as ``shot_planner.plan_shots`` computes them; every strategy then picks
view sets from that same pool, so the labels span the quality range on identical geometry:

    random           uniform draws from the planner's candidates
    iso              the single whole-part ISO render the agent used to get (k=1)
    fixed4           the front/right/top/iso sheet (k=4)
    hand_greedy      greedy on hand_utility (the planner's fallback)
    adversarial_low  greedy on -hand_utility (deliberately poor sets)
    model_greedy     greedy on the learned UtilityModel (when weights exist)

``run_trials`` stores each set's tokens and hands the sets to
``training.viewpoint.observer.render`` for the grid images (``shot_planner.render_shot_grid``
in worker processes — the very pixels the agent receives). ``observe`` then asks the
cross-fitted observer (``observer.judge.label``) for P(correct) of every (set, query) pair:
the soft target the utility model is trained on.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .store import JUDGES, TrialStore, sha256_file

TRAINING_STRATEGIES = ("random", "iso", "fixed4", "hand_greedy", "adversarial_low", "model_greedy")
AB_ARMS = (("model_greedy", 4), ("hand_greedy", 4), ("iso", 1), ("fixed4", 4), ("random", 4))
_ISO = (1.0 / math.sqrt(2.64), 1.0 / math.sqrt(2.64), 0.8 / math.sqrt(2.64))
FIXED_DIRS = {"front": (0.0, -1.0, 0.0), "right": (1.0, 0.0, 0.0), "top": (0.0, 0.0, 1.0),
              "iso": _ISO}
# Parts measured ahead of the renderer: enough to keep the render workers fed across part
# boundaries without holding many parts' metrics in memory.
_RENDER_BACKLOG = 2


def _sha(*parts) -> str:
    return hashlib.sha256(json.dumps([str(p) for p in parts]).encode()).hexdigest()


# ---------------------------------------------------------------------------- config

@dataclass
class TrialConfig:
    splits: tuple = ("train", "val", "test")
    targets_per_part: int = 1
    random_sets: int = 2
    k_values: tuple = (1, 4)
    strategies: tuple = TRAINING_STRATEGIES
    seed: int = 0
    tile: tuple = (512, 384)
    weights: Optional[str] = None
    limit_parts: Optional[int] = None
    budget_minutes: Optional[float] = None
    render_workers: int = 8
    namespace: str = "trials"   # store namespace ("ab" = held-out A/B sets)
    ab_arms: tuple = ()         # non-empty => A/B mode: exactly these (strategy, k) arms

    def digest(self, weights_sha: Optional[str] = None) -> str:
        keep = {k: v for k, v in asdict(self).items()
                if k not in ("splits", "limit_parts", "budget_minutes", "render_workers",
                             "weights")}
        keep["weights_sha"] = weights_sha
        return _sha(json.dumps(keep, sort_keys=True, default=str))[:16]


# ---------------------------------------------------------------------------- engine side

def load_labels(path, splits) -> list:
    want = set(splits)
    recs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("schema") != "chudware.geomlabel/1":
                raise ValueError(f"{path}: record {r.get('part_id')!r} has schema "
                                 f"{r.get('schema')!r}, expected chudware.geomlabel/1")
            if r["split"] in want:
                recs.append(r)
    # Hash order spreads a --limit-parts subset across datasets instead of taking one source.
    recs.sort(key=lambda r: hashlib.sha1(str(r["part_id"]).encode()).hexdigest())
    return recs


class PartDoc:
    """The part imported into the engine exactly as the labels were mapped (import_file)."""

    def __init__(self, record: dict):
        self.record = record
        self.doc_id: Optional[str] = None
        self.faces: list = []

    def __enter__(self) -> "PartDoc":
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        self.doc_id = cb.call("import_file", {"path": self.record["file"], "format": "step"})["doc_id"]
        self.faces = cb.call("doc_query", {"doc_id": self.doc_id,
                                           "query": {"of": "face"}}).get("matches") or []
        want = int((self.record.get("stats") or {}).get("n_faces", len(self.faces)))
        if len(self.faces) != want:
            raise ValueError(f"part {self.record['part_id']}: engine import has "
                             f"{len(self.faces)} faces, labels expect {want}")
        return self

    def __exit__(self, *exc) -> None:
        if self.doc_id is None:
            return
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        try:
            cb.call("doc_close", {"doc_id": self.doc_id})
        except Exception as e:  # noqa: BLE001 — a failed close must not hide the real error
            print(f"[trials] doc_close {self.doc_id} failed: {e}", file=sys.stderr)


@dataclass
class Universe:
    """Candidates + metrics for one (part, target) — computed once, shared by all strategies.
    Views [0, n_planner) are the planner's own candidates; the fixed sheet cameras follow."""
    mode: str
    target_instance: int
    target_faces: tuple
    target_label: Optional[str]
    cands: list
    metrics: dict
    roles: Optional[dict]
    context: dict
    n_planner: int
    fixed: dict = field(default_factory=dict)

    def planner_metrics(self) -> dict:
        """The metrics restricted to the planner's candidates (what plan_shots selects from)."""
        return {**self.metrics, "views": self.metrics["views"][:self.n_planner]}


def build_universe(part: PartDoc, *, target_faces: tuple = (), target_instance: int = -1,
                   target_label: Optional[str] = None, tile: tuple) -> Universe:
    """plan_shots' candidate pool and measurements for the whole part or one target, plus
    the fixed front/right/top/iso cameras measured after them."""
    from services.mcp import cad_common as cc  # noqa: PLC0415
    from services.mcp import shot_planner as sp  # noqa: PLC0415
    from services.mcp.cad_viewpoint import perspective_camera  # noqa: PLC0415
    from services.viewpoint.utility.features import (  # noqa: PLC0415
        context_from, target_visible_fraction)

    W, H = int(tile[0]), int(tile[1])
    doc = part.doc_id
    part_bb = cc.cb.call("doc_bbox", {"doc_id": doc}) or {}
    if not part_bb.get("size"):
        raise ValueError(f"part {part.record['part_id']}: the document has no geometry")
    matches = [part.faces[int(o)] for o in target_faces]
    internal = bool(matches) and bool(cc._target_is_internal(matches, part_bb))
    if matches:
        tbox = cc._target_bbox(doc, matches)
        normals = [m.get("normal") or m.get("axis") for m in matches
                   if isinstance(m.get("normal") or m.get("axis"), (list, tuple))]
        pts = sp.target_points(doc, matches)
        cands = sp.candidate_cameras(part_bb, target_bbox=tbox, target_normals=normals,
                                     target_points=pts, internal=internal, width=W, height=H)
    else:
        cands = sp.candidate_cameras(part_bb, part_points=sp.part_points(doc), width=W, height=H)
    metrics = sp.view_metrics(doc, cands)
    roles = sp._roles(metrics, matches, None) if matches else None
    if matches and not internal and roles["target"]:
        seen = max((target_visible_fraction(metrics, v, roles) or 0.0)
                   for v in range(len(cands)))
        if seen < sp._SEE_ENOUGH:
            extra = sp.section_candidates(part_bb, tbox, normals, target_points=pts)
            sp._extend(doc, metrics, extra)
            cands += extra
    n_planner = len(cands)
    fixed_cands = [{"camera": perspective_camera(part_bb, d, W, H, scene_bbox=part_bb),
                    "dir": [float(x) for x in d], "label": name, "section": None}
                   for name, d in FIXED_DIRS.items()]
    sp._extend(doc, metrics, fixed_cands)
    fixed = {name: n_planner + j for j, name in enumerate(FIXED_DIRS)}
    cands += fixed_cands
    context = context_from(metrics, roles=roles, target_kind="feature" if matches else "part",
                           internal=internal, part_bbox=part_bb)
    return Universe(mode="target" if matches else "part", target_instance=int(target_instance),
                    target_faces=tuple(int(o) for o in target_faces), target_label=target_label,
                    cands=cands, metrics=metrics, roles=roles, context=context,
                    n_planner=n_planner, fixed=fixed)


def strategy_sets(u: Universe, *, strategies, k_values, random_sets: int, rng: random.Random,
                  model=None, arms: tuple = ()) -> list:
    """[(strategies tuple, k, view indices in pick order)], identical index lists merged.
    Greedy strategies select from the planner's candidates only, as plan_shots does."""
    from services.mcp.shot_planner import greedy_select, set_scorer  # noqa: PLC0415

    pm = u.planner_metrics()
    hand = set_scorer(pm, u.roles, u.context, None)
    learned = set_scorer(pm, u.roles, u.context, model) if model is not None else None
    pool = list(range(u.n_planner))

    def greedy(score: Callable, k: int) -> list:
        return [int(p["index"]) for p in greedy_select(pm, score, k)]

    def make(strategy: str, k: int) -> Optional[list]:
        if strategy == "random":
            return rng.sample(pool, min(k, len(pool)))
        if strategy == "iso":
            return [u.fixed["iso"]] if k == 1 else None
        if strategy == "fixed4":
            return [u.fixed[n] for n in ("front", "right", "top", "iso")] if k == 4 else None
        if strategy == "hand_greedy":
            return greedy(hand, k)
        if strategy == "adversarial_low":
            return greedy(lambda s: -hand(s), k)
        if strategy == "model_greedy":
            return greedy(learned, k) if learned is not None else None
        raise ValueError(f"unknown strategy {strategy!r}")

    if arms:
        wanted = list(arms)
    else:
        wanted = [(s, k) for k in k_values for s in strategies
                  for _rep in range(random_sets if s == "random" else 1)]
    merged: dict = {}
    order: list = []
    for s, k in wanted:
        idx = make(s, k)
        if not idx:
            continue
        key = tuple(idx)
        if key not in merged:
            merged[key] = [s]
            order.append(key)
        elif s not in merged[key]:
            merged[key].append(s)
    return [(tuple(merged[key]), len(key), list(key)) for key in order]


def set_key_of(part_id: str, u: Universe, idx: list, tile: tuple) -> str:
    """Content key of a view set: part, target, exact cameras/sections, tile, feature schema."""
    from services.viewpoint.utility.features import FEATURE_SCHEMA  # noqa: PLC0415
    views = [[u.cands[i]["camera"], u.cands[i]["section"]] for i in idx]
    return _sha(part_id, u.mode, u.target_instance, json.dumps(views, sort_keys=True, default=str),
                list(tile), FEATURE_SCHEMA)[:24]


def load_model(weights: Optional[str], *, required: bool):
    """(UtilityModel, path) for model_greedy, or (None, None) when no usable weights exist."""
    from services.viewpoint.utility.model import DEFAULT_WEIGHTS, UtilityModel  # noqa: PLC0415

    path = Path(weights) if weights else DEFAULT_WEIGHTS
    if not path.exists():
        if required:
            raise FileNotFoundError(f"model weights not found: {path}")
        return None, None
    try:
        return UtilityModel.load(str(path)), path
    except ValueError as exc:
        if required:
            raise
        print(f"[trials] model_greedy disabled: {path} refused ({exc})", file=sys.stderr)
        return None, None


def _queries_by_part(queries) -> dict:
    out: dict = {}
    for q in queries:
        out.setdefault(str(q.part_id), []).append(q)
    return out


def part_sets(part: PartDoc, rec: dict, queries: list, cfg: TrialConfig, store: TrialStore, *,
              model=None, model_path: Optional[Path] = None) -> tuple:
    """(set records without images, observer ViewSets) for one part: the whole-part universe
    when part-mode queries exist, plus one universe per picked target."""
    from services.viewpoint.utility.features import (  # noqa: PLC0415
        FEATURE_SCHEMA, build_set_tokens, hand_utility)
    from training.viewpoint.observer.queries import pick_targets  # noqa: PLC0415
    from training.viewpoint.observer.render import ViewSet  # noqa: PLC0415

    pid = str(rec["part_id"])
    rng = random.Random(f"{cfg.seed}:{pid}")
    part_q = [q for q in queries if q.mode == "part"]
    groups = [(-1, (), None, [q.qid for q in part_q])] if part_q else []
    target_q = [q for q in queries if q.mode == "target"]
    for inst in pick_targets(target_q, int(cfg.targets_per_part), seed=cfg.seed):
        mine = [q for q in target_q if q.target_instance == inst]
        cls = next((f.get("class") for f in rec.get("features") or ()
                    if f.get("instance") == inst), None)
        groups.append((int(inst), tuple(mine[0].target_faces),
                       str(cls).replace("_", " ") if cls else "target", [q.qid for q in mine]))
    records, view_sets = [], []
    for inst, faces, label, qids in groups:
        u = build_universe(part, target_faces=faces, target_instance=inst, target_label=label,
                           tile=cfg.tile)
        for strategies, k, idx in strategy_sets(
                u, strategies=cfg.strategies, k_values=cfg.k_values,
                random_sets=cfg.random_sets, rng=rng, model=model, arms=cfg.ab_arms):
            key = set_key_of(pid, u, idx, cfg.tile)
            tokens = build_set_tokens(u.metrics, idx, u.roles, u.context)
            records.append({
                "set_key": key, "part_id": pid, "split": rec["split"],
                "namespace": cfg.namespace, "mode": u.mode,
                "target_instance": u.target_instance, "strategies": list(strategies), "k": k,
                "view_indices": idx, "view_labels": [u.cands[i]["label"] for i in idx],
                "view_dirs": [u.cands[i]["dir"] for i in idx],
                "sections": [u.cands[i]["section"] is not None for i in idx],
                "hand_utility": float(hand_utility(u.metrics, idx, u.roles)),
                "feature_schema": FEATURE_SCHEMA, "n_faces": int(len(tokens["face_mask"])),
                "tokens_path": store.save_tokens(key, tokens), "qids": list(qids),
                "model_weights": str(model_path) if model_path else None,
            })
            view_sets.append(ViewSet(
                set_key=key,
                views=tuple({"camera": u.cands[i]["camera"], "dir": u.cands[i]["dir"],
                             "label": u.cands[i]["label"], "section": u.cands[i]["section"]}
                            for i in idx),
                target_faces=u.target_faces, target_label=u.target_label,
                tile=tuple(cfg.tile)))
    return records, view_sets


# ---------------------------------------------------------------------------- run loop

def run_trials(labels_path, store: TrialStore, cfg: TrialConfig, *,
               part_filter: Optional[Callable[[dict], bool]] = None) -> dict:
    """Build every strategy's view sets for each part, store their tokens, render their grids
    through the observer's render workers and append the set records.

    Engine measurement stays on this thread (the in-process engine is not re-entrant);
    rendering runs in the observer's worker processes, driven from one helper thread, while
    the next part is measured. A part's set records are written only once its grids exist,
    and the part is marked done only then, so an interrupted run resumes cleanly."""
    from training.viewpoint.observer.queries import build_query_bank  # noqa: PLC0415
    from training.viewpoint.observer.render import IMAGE_KEY, RenderPool, render_sets  # noqa: PLC0415

    t0 = time.monotonic()
    everything = load_labels(labels_path, ("train", "val", "test"))
    # Answer balancing runs over the whole manifest, so a part's queries do not depend on
    # --splits / --limit-parts (a small subset would balance most of them away).
    bank = build_query_bank(everything, seed=cfg.seed)
    store.save_queries(bank)
    by_part = _queries_by_part(bank)
    records = [r for r in everything if r["split"] in set(cfg.splits)]
    if part_filter is not None:
        records = [r for r in records if part_filter(r)]
    if cfg.limit_parts is not None:
        records = records[: int(cfg.limit_parts)]
    need_model = "model_greedy" in (cfg.strategies if not cfg.ab_arms else
                                    tuple(s for s, _k in cfg.ab_arms))
    model, model_path = (load_model(cfg.weights, required=bool(cfg.ab_arms or cfg.weights))
                         if need_model else (None, None))
    cfg_sha = cfg.digest(sha256_file(model_path) if model_path else None)
    done = store.done_parts(cfg_sha)
    deadline = (t0 + 60.0 * cfg.budget_minutes) if cfg.budget_minutes else None
    skipped_parts: list = []
    counts = {"parts_done": 0, "sets": 0, "no_queries": 0, "already_done": 0}
    pending: list = []   # [(pid, set records, Future[list[Path]])]

    def finish(pid: str, recs: list, fut: Future) -> None:
        try:
            paths = fut.result()
        except Exception as exc:  # noqa: BLE001 — one unrenderable part must not end a long run
            skipped_parts.append({"part_id": pid, "stage": "render",
                                  "error": f"{type(exc).__name__}: {exc}"})
            print(f"[trials] skip part {pid} (render): {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return
        if len(paths) != len(recs):
            raise RuntimeError(f"render_sets returned {len(paths)} images for {len(recs)} sets")
        for r, p in zip(recs, paths):
            p = Path(p)
            r[IMAGE_KEY] = str(p.relative_to(store.root))
            r["image_sha"] = p.stem
        store.append_sets(recs)
        store.mark_part_done(pid, cfg_sha)
        counts["parts_done"] += 1
        counts["sets"] += len(recs)

    with RenderPool(max(1, int(cfg.render_workers))) as pool, \
            ThreadPoolExecutor(max_workers=1) as renderer:
        for rec in records:
            if deadline is not None and time.monotonic() > deadline:
                break
            pid = str(rec["part_id"])
            if pid in done:
                counts["already_done"] += 1
                continue
            queries = by_part.get(pid) or []
            if not queries:
                counts["no_queries"] += 1
                continue
            try:
                with PartDoc(rec) as part:
                    recs, view_sets = part_sets(part, rec, queries, cfg, store, model=model,
                                                model_path=model_path)
            except Exception as exc:  # noqa: BLE001 — one bad STEP must not end a long run
                skipped_parts.append({"part_id": pid, "stage": "measure",
                                      "error": f"{type(exc).__name__}: {exc}"})
                print(f"[trials] skip part {pid}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if not recs:
                continue
            pending.append((pid, recs, renderer.submit(render_sets, rec, view_sets,
                                                       store=store, pool=pool)))
            while len(pending) > _RENDER_BACKLOG:
                finish(*pending.pop(0))
        while pending:
            finish(*pending.pop(0))
    seconds = time.monotonic() - t0
    return {"parts": len(records), **counts, "skipped_parts": skipped_parts,
            "queries": len(bank), "seconds": round(seconds, 1),
            "sets_per_s": round(counts["sets"] / seconds, 3) if seconds > 0 else None,
            "config_sha": cfg_sha, "store": str(store.root), "namespace": cfg.namespace,
            "model_weights": str(model_path) if model_path else None}


# ---------------------------------------------------------------------------- labels

def observe(store: TrialStore, *, judge: str = "crossfit", splits=None, chunk: int = 20000,
            relabel: bool = False) -> dict:
    """Label every (set, query) pair of the store's namespace with the observer `judge`
    ("crossfit": the fold that never trained on the part — training labels; "independent":
    the second cross-fit observer — A/B scoring and the label-noise floor only)."""
    from training.viewpoint.observer import judge as observer_judge  # noqa: PLC0415

    if judge not in JUDGES:
        raise ValueError(f"unknown judge {judge!r}; expected one of {JUDGES}")
    t0 = time.monotonic()
    want = set(splits) if splits else None
    have = set() if relabel else set(store.load_labels(judge))
    pairs = sorted((sk, q) for sk, rec in store.load_sets().items()
                   if want is None or rec["split"] in want
                   for q in rec.get("qids") or ()
                   if (sk, q) not in have)
    n_rows, p_sum, c_sum = 0, 0.0, 0.0
    for start in range(0, len(pairs), max(1, int(chunk))):
        batch = pairs[start:start + chunk]
        rows = observer_judge.label(store, [sk for sk, _q in batch], [q for _s, q in batch],
                                    judge=judge)
        store.append_labels(judge, rows)
        n_rows += len(rows)
        p_sum += sum(float(r["p_correct"]) for r in rows)
        c_sum += sum(1.0 for r in rows if r["correct"])
    return {"judge": judge, "namespace": store.name, "labelled": n_rows,
            "already_labelled": len(have), "mean_p_correct": p_sum / n_rows if n_rows else None,
            "accuracy": c_sum / n_rows if n_rows else None,
            "seconds": round(time.monotonic() - t0, 1)}
