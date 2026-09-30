"""Render view sets EXACTLY as the agent receives them, in parallel worker processes.

Every image goes through `services.mcp.shot_planner.render_shot_grid` (the renderer the
agent's `plan_shots` grids use), with candidate cameras from `candidate_cameras` and, for a
target, the planner's own roles (`_roles`: target faces + the context ring that orders the SID
tags) and emphasis overlay. Nothing here downsamples: the Observer model does that itself.

Each worker process loads its OWN in-process engine (CHUDWARE_ENGINE_DAEMON=0 +
CHUDWARE_LIBCHUDWARE), never the user's hostd, and never touches the calling process's engine.
Outputs land in a private temp dir per worker (the durable render cache the GUI shows is not
used) and are copied into the trial store's content-addressed ``images/<sha>.png``; a
render-key index (``render_index/<key[:2]>/<key>`` -> sha) makes re-runs skip rendering. The
render key covers the part, the exact cameras/sections/target/tile AND a digest of the renderer
(shot_planner.py, cad_common.py, the engine library), so a renderer change re-renders.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from training.viewpoint.durable import append_jsonl, durable_replace, nonempty, read_jsonl

RENDER_SCHEMA = "viewnet.render/1"
IMAGE_KEY = "image"                 # set-record field: grid path relative to the store root
TILE = (512, 384)
_REPO = Path(__file__).resolve().parents[3]
_DEFAULT_LIB = _REPO / "core" / "build-linux" / "bin" / "libchudware.so"


class RenderError(RuntimeError):
    """A view set could not be rendered (import mismatch, engine refusal, worker crash)."""


@dataclass(frozen=True)
class ViewSet:
    set_key: str
    views: tuple                      # shot_planner candidate items {camera, dir, label, section}
    target_faces: tuple = ()          # engine face ordinals painted by the emphasis overlay
    target_label: Optional[str] = None
    tile: tuple = TILE


@dataclass(frozen=True)
class ObserverSetConfig:
    """How many view sets `observer render` draws per part for Observer training: mostly
    uniform-random candidate subsets (the Observer must learn to read ANY view), plus the hand
    planner's greedy pick so good views are represented too."""
    random_k1: int = 3
    random_k4: int = 3
    target_random_k1: int = 2
    target_random_k4: int = 2
    greedy: bool = True
    targets_per_part: int = 2
    tile: tuple = TILE
    seed: int = 0


def default_workers() -> int:
    env = os.environ.get("CHUDWARE_VIEWNET_RENDER_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, (os.cpu_count() or 4) - 2)


def _sha_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _lib_path() -> Path:
    return Path(os.environ.get("CHUDWARE_LIBCHUDWARE") or _DEFAULT_LIB)


@lru_cache(maxsize=1)
def renderer_digest() -> str:
    h = hashlib.sha256(RENDER_SCHEMA.encode())
    for rel in ("services/mcp/shot_planner.py", "services/mcp/cad_common.py",
                "services/mcp/cad_viewpoint.py"):
        h.update((_REPO / rel).read_bytes())
    lib = _lib_path()
    st = lib.stat()
    h.update(f"{lib.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def render_key(record: dict, vs: ViewSet) -> str:
    return _sha_json([RENDER_SCHEMA, renderer_digest(), str(record["part_id"]),
                      str(record["file"]), int(record["stats"]["n_faces"]),
                      [{"camera": v["camera"], "dir": v["dir"], "label": v.get("label"),
                        "section": v.get("section")} for v in vs.views],
                      sorted(int(o) for o in vs.target_faces), list(vs.tile)])


# ---------------------------------------------------------------------------- store side

def _index_path(root: Path, key: str) -> Path:
    return root / "render_index" / key[:2] / key


def _cached(root: Path, key: str) -> Optional[Path]:
    idx = _index_path(root, key)
    if not nonempty(idx):
        return None
    img = root / "images" / f"{idx.read_text().strip()}.png"
    return img if nonempty(img) else None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    durable_replace(tmp, path)


def _store_image(root: Path, key: str, png: str) -> Path:
    """TrialStore.save_image's layout (images/<content sha>.png) + the render-key index."""
    data = Path(png).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    dest = root / "images" / f"{sha}.png"
    if not nonempty(dest):
        _atomic_write(dest, data)
    _atomic_write(_index_path(root, key), sha.encode())
    return dest


def image_rel(root, path) -> str:
    return str(Path(path).relative_to(Path(root).expanduser()))


# ---------------------------------------------------------------------------- worker side

_W: dict = {}


def _init_worker(repo: str) -> None:
    import atexit  # noqa: PLC0415
    import faulthandler  # noqa: PLC0415 — an engine crash names its native frame in the log
    faulthandler.enable()
    os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"
    os.environ.setdefault("CHUDWARE_LIBCHUDWARE", str(_DEFAULT_LIB))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    tmp = tempfile.mkdtemp(prefix="viewnet_render_")
    atexit.register(shutil.rmtree, tmp, True)
    tempfile.tempdir = tmp
    os.environ["TMPDIR"] = tmp          # the engine's file fallback for the handoff
    from services.mcp import cad_common as cc  # noqa: PLC0415

    # The durable render cache is what the GUI shows the user; training grids stay private.
    def _private_output(stem: str = "render", suffix: str = ".png") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="grid_", dir=tmp)
        os.close(fd)
        return path

    cc.render_output_path = _private_output
    _W.update(tmp=tmp, part=None, doc=None, faces=None, bbox=None, roles={})


def _wipe_tmp() -> None:
    tmp = _W["tmp"]
    for name in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, name))
        except OSError:
            shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)


def _open_part(record: dict) -> None:
    from services.mcp import core_bridge as cb  # noqa: PLC0415

    if _W["part"] == record["part_id"]:
        return
    if _W["doc"] is not None:
        try:
            cb.call("doc_close", {"doc_id": _W["doc"]})
        except Exception as e:  # noqa: BLE001 — a failed close must not hide the next part
            print(f"[observer.render] doc_close {_W['doc']} failed: {e}", file=sys.stderr)
    _W.update(part=None, doc=None, faces=None, bbox=None, roles={})
    doc = cb.call("import_file", {"path": record["file"], "format": "step"})["doc_id"]
    _W["doc"] = doc
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}}).get("matches") or []
    want = int(record["stats"]["n_faces"])
    if len(faces) != want:
        raise RenderError(f"part {record['part_id']}: engine import has {len(faces)} faces, "
                          f"labels expect {want}")
    _W.update(part=record["part_id"], faces=faces,
              bbox=cb.call("doc_bbox", {"doc_id": doc}))


def _target(target_faces: tuple) -> dict:
    """Matches, fit points, normals, internal flag of a target (memoised per open part)."""
    from services.mcp import cad_common as cc  # noqa: PLC0415
    from services.mcp import shot_planner as sp  # noqa: PLC0415

    key = tuple(sorted(int(o) for o in target_faces))
    if key in _W["roles"]:
        return _W["roles"][key]
    faces = _W["faces"]
    bad = [o for o in key if not 0 <= o < len(faces)]
    if bad:
        raise RenderError(f"part {_W['part']}: target face ordinals {bad} out of range")
    matches = [faces[o] for o in key]
    normals = [m.get("normal") or m.get("axis") for m in matches
               if isinstance(m.get("normal") or m.get("axis"), (list, tuple))]
    t = {"matches": matches, "bbox": cc._target_bbox(_W["doc"], matches),
         "points": sp.target_points(_W["doc"], matches), "normals": normals,
         "internal": bool(cc._target_is_internal(matches, _W["bbox"])), "ordinals": list(key)}
    _W["roles"][key] = t
    return t


def _roles_for(t: dict, metrics: dict) -> dict:
    from services.mcp import shot_planner as sp  # noqa: PLC0415

    roles = sp._roles(metrics, t["matches"], None)
    if not roles["target"]:
        roles["target"] = list(t["ordinals"])
    return roles


def _render_one(vs: ViewSet, root: Path, key: str, roles: Optional[dict]) -> Path:
    from services.mcp import shot_planner as sp  # noqa: PLC0415

    plan = {"views": [dict(v) for v in vs.views], "part_bbox": _W["bbox"]}
    if vs.target_faces:
        t = _target(vs.target_faces)
        if roles is None:
            roles = _roles_for(t, sp.view_metrics(_W["doc"], plan["views"]))
        plan["target"] = {"matches": t["matches"], "label": vs.target_label or "target",
                          "bbox": t["bbox"]}
        plan["roles"] = roles
    try:
        png, _info = sp.render_shot_grid(_W["doc"], plan, tile=tuple(vs.tile))
        return _store_image(root, key, png)
    finally:
        _wipe_tmp()


def _job_render(record: dict, sets: list, keys: list, root: str) -> list:
    """[(set_key, image path | None, error | None)] for view sets handed in by the caller."""
    rootp = Path(root)
    out = []
    try:
        _open_part(record)
    except Exception as e:  # noqa: BLE001 — the whole part fails, reported per set
        return [(vs.set_key, None, f"{type(e).__name__}: {e}") for vs in sets]
    for vs, key in zip(sets, keys):
        try:
            out.append((vs.set_key, str(_render_one(vs, rootp, key, None)), None))
        except Exception as e:  # noqa: BLE001
            out.append((vs.set_key, None, f"{type(e).__name__}: {e}"))
    return out


def _random_sets(rng: random.Random, pool: list, k: int, n: int) -> list:
    return [sorted(rng.sample(pool, k)) for _ in range(n) if len(pool) >= k]


def _job_observer(record: dict, targets: list, cfg: ObserverSetConfig, root: str) -> dict:
    """Plan + render the Observer's own training sets for one part (part mode + targets).
    Returns {"sets": [set records], "errors": [str], "rendered": n, "cached": n}."""
    from services.mcp import shot_planner as sp  # noqa: PLC0415
    from services.viewpoint.utility import features as uf  # noqa: PLC0415

    rootp = Path(root)
    res = {"sets": [], "errors": [], "rendered": 0, "cached": 0}
    try:
        _open_part(record)
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"{record['part_id']}: {type(e).__name__}: {e}")
        return res
    doc, part_bb = _W["doc"], _W["bbox"]
    feats = {int(f["instance"]): f for f in record.get("features") or []}
    universes = [("part", -1)] + [("target", int(i)) for i in targets if int(i) in feats]
    for mode, inst in universes:
        try:
            if mode == "part":
                cands = sp.candidate_cameras(part_bb, part_points=sp.part_points(doc),
                                             width=cfg.tile[0], height=cfg.tile[1])
                metrics = sp.view_metrics(doc, cands)
                roles, t, label, tfaces = None, None, None, ()
                context = uf.context_from(metrics, roles=None, target_kind="part",
                                          internal=False, part_bbox=part_bb)
                n1, n4 = cfg.random_k1, cfg.random_k4
            else:
                f = feats[inst]
                tfaces = tuple(sorted(int(o) for o in f["faces"]))
                t = _target(tfaces)
                label = str(f["class"])
                cands = sp.candidate_cameras(part_bb, target_bbox=t["bbox"],
                                             target_normals=t["normals"],
                                             target_points=t["points"], internal=t["internal"],
                                             width=cfg.tile[0], height=cfg.tile[1])
                metrics = sp.view_metrics(doc, cands)
                roles = _roles_for(t, metrics)
                # plan_shots' rule: when no uncut view sees half the target, cuts join.
                if not t["internal"] and roles["target"]:
                    seen = max((uf.target_visible_fraction(metrics, v, roles) or 0.0)
                               for v in range(len(cands)))
                    if seen < sp._SEE_ENOUGH:
                        extra = sp.section_candidates(part_bb, t["bbox"], t["normals"],
                                                      target_points=t["points"],
                                                      width=cfg.tile[0], height=cfg.tile[1])
                        sp._extend(doc, metrics, extra)
                        cands += extra
                context = uf.context_from(metrics, roles=roles, target_kind="feature",
                                          internal=t["internal"], part_bbox=part_bb)
                n1, n4 = cfg.target_random_k1, cfg.target_random_k4
            rng = random.Random(int(_sha_json([cfg.seed, record["part_id"], mode, inst])[:16], 16))
            pool = list(range(len(cands)))
            picks: list = [("random", s) for s in _random_sets(rng, pool, 1, n1)]
            picks += [("random", s) for s in _random_sets(rng, pool, 4, n4)]
            if cfg.greedy:
                score = sp.set_scorer(metrics, roles, context, None)
                for k in (1, 4):
                    picks.append(("hand_greedy",
                                  [int(p["index"]) for p in sp.greedy_select(metrics, score, k)]))
        except Exception as e:  # noqa: BLE001 — one universe failing must not drop the part
            res["errors"].append(f"{record['part_id']} {mode} {inst}: {type(e).__name__}: {e}")
            continue
        merged: dict = {}
        for strategy, idx in picks:
            merged.setdefault(tuple(idx), [])
            if strategy not in merged[tuple(idx)]:
                merged[tuple(idx)].append(strategy)
        for idx, strategies in merged.items():
            views = tuple({"camera": cands[i]["camera"], "dir": list(cands[i]["dir"]),
                           "label": cands[i]["label"], "section": cands[i]["section"]}
                          for i in idx)
            vs = ViewSet(set_key="", views=views, target_faces=tfaces, target_label=label,
                         tile=tuple(cfg.tile))
            key = render_key(record, vs)
            try:
                img = _cached(rootp, key)
                if img is None:
                    img = _render_one(vs, rootp, key, roles)
                    res["rendered"] += 1
                else:
                    res["cached"] += 1
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"{record['part_id']} {mode} {inst} {list(idx)}: "
                                     f"{type(e).__name__}: {e}")
                continue
            res["sets"].append({
                "set_key": "obs-" + key[:24], "render_key": key,
                "part_id": record["part_id"], "split": record["split"], "mode": mode,
                "target_instance": inst, "k": len(idx), IMAGE_KEY: image_rel(rootp, img),
                "strategies": strategies, "view_labels": [v["label"] for v in views],
                "sections": [bool(v["section"]) for v in views]})
    return res


# ---------------------------------------------------------------------------- pool

class RenderPool:
    """Spawned worker processes, each with its own in-process engine. Reuse one pool across
    parts: a worker keeps its last part open, and jobs are whole parts or chunks of one."""

    def __init__(self, workers: Optional[int] = None, *, max_tasks_per_child: int = 400,
                 stall_s: Optional[float] = None):
        self.workers = int(workers or default_workers())
        self.max_tasks_per_child = max_tasks_per_child
        # No job finishing for this long means the pool is wedged, not slow: the 2026-09-29
        # campaign sat 2 h on a parent whose workers had all vanished (as_completed never
        # returns once the executor stops noticing its dead children). One part renders in
        # well under a minute; 10 min is generous for the slowest.
        self.stall_s = float(stall_s if stall_s is not None
                             else os.environ.get("CHUDWARE_VIEWNET_RENDER_STALL_S", "600"))
        self._ex: Optional[ProcessPoolExecutor] = None

    def _executor(self) -> ProcessPoolExecutor:
        if self._ex is None:
            import multiprocessing as mp  # noqa: PLC0415
            self._ex = ProcessPoolExecutor(
                max_workers=self.workers, mp_context=mp.get_context("spawn"),
                initializer=_init_worker, initargs=(str(_REPO),),
                max_tasks_per_child=self.max_tasks_per_child)
        return self._ex

    def run(self, fn, jobs: list):
        """Yield (job, result | exception) as jobs complete. A crashed worker (engine
        segfault) breaks the pool and every job still in it, and a pool with no completion for
        `stall_s` is treated the same way: the pool is killed and rebuilt and the unfinished
        jobs retried once; a job that breaks it twice is yielded with its exception."""
        from concurrent.futures import FIRST_COMPLETED, wait  # noqa: PLC0415
        pending = list(jobs)
        attempts: dict = {}
        while pending:
            ex = self._executor()
            futs = {ex.submit(fn, *job): i for i, job in enumerate(pending)}
            retry: list = []
            broken = False

            def _fail(job, err) -> Optional[tuple]:
                n = attempts.get(id(job), 0) + 1
                attempts[id(job)] = n
                if n >= 2:
                    return job, err
                retry.append(job)
                return None

            waiting = set(futs)
            while waiting:
                done, waiting = wait(waiting, timeout=self.stall_s, return_when=FIRST_COMPLETED)
                if not done:
                    broken = True
                    err = BrokenProcessPool(f"render pool stalled: no job finished in "
                                            f"{self.stall_s:.0f} s")
                    for fut in waiting:
                        out = _fail(pending[futs[fut]], err)
                        if out:
                            yield out
                    break
                for fut in done:
                    job = pending[futs[fut]]
                    try:
                        res = fut.result()
                    except BrokenProcessPool as e:
                        broken = True
                        out = _fail(job, e)
                        if out:
                            yield out
                        continue
                    except Exception as e:  # noqa: BLE001
                        res = e
                    yield job, res
            if broken:
                self.close(kill=True)
            pending = retry

    def close(self, *, kill: bool = False) -> None:
        if self._ex is not None:
            if kill:
                # A wedged pool never drains: kill its workers so shutdown cannot block.
                for proc in list((getattr(self._ex, "_processes", None) or {}).values()):
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                self._ex.shutdown(wait=False, cancel_futures=True)
            else:
                self._ex.shutdown(wait=True, cancel_futures=True)
            self._ex = None

    def __enter__(self) -> "RenderPool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _root(store) -> Path:
    return Path(store.root).expanduser()


def _log_failures(root: Path, rows: list) -> None:
    if not rows:
        return
    p = root / "observer" / "render_failures.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    for r in rows[:5]:
        print(f"[observer.render] {r['part_id']} {r.get('set_key', '')}: {r['error']}",
              file=sys.stderr)


def _chunks(items: list, n: int) -> list:
    return [items[i:i + n] for i in range(0, len(items), n)] if items else []


def render_many(jobs: Iterable, *, store, workers: Optional[int] = None,
                pool: Optional[RenderPool] = None, chunk: int = 6) -> dict:
    """{set_key: image path} for [(record, [ViewSet])] jobs. Cached renders are not redone.
    Failed sets are absent from the result and logged to observer/render_failures.jsonl."""
    root = _root(store)
    out: dict = {}
    work: list = []
    for record, sets in jobs:
        miss = []
        for vs in sets:
            key = render_key(record, vs)
            hit = _cached(root, key)
            if hit is not None:
                out[vs.set_key] = hit
            else:
                miss.append((vs, key))
        for part in _chunks(miss, chunk):
            work.append((record, [m[0] for m in part], [m[1] for m in part], str(root)))
    if not work:
        return out
    own = pool is None
    pool = pool or RenderPool(min(workers or default_workers(), len(work)))
    failures = []
    try:
        for job, res in pool.run(_job_render, work):
            record = job[0]
            if isinstance(res, BaseException):
                failures += [{"part_id": record["part_id"], "set_key": vs.set_key,
                              "error": f"{type(res).__name__}: {res}"} for vs in job[1]]
                continue
            for set_key, path, err in res:
                if path is not None:
                    out[set_key] = Path(path)
                else:
                    failures.append({"part_id": record["part_id"], "set_key": set_key,
                                     "error": err})
    finally:
        if own:
            pool.close()
    _log_failures(root, failures)
    return out


def render_sets(record: dict, sets: list, *, store, pool: Optional[RenderPool] = None) -> list:
    """Image paths (same order as `sets`) for one part's view sets; raises RenderError when
    any set failed."""
    got = render_many([(record, list(sets))], store=store, pool=pool,
                      chunk=max(1, -(-len(sets) // (pool.workers if pool else default_workers()))))
    missing = [vs.set_key for vs in sets if vs.set_key not in got]
    if missing:
        raise RenderError(f"part {record['part_id']}: {len(missing)} view set(s) failed to "
                          f"render (see observer/render_failures.jsonl): {missing[:4]}")
    return [got[vs.set_key] for vs in sets]


# ---------------------------------------------------------------------------- observer sets

def observer_sets_path(store_root) -> Path:
    return Path(store_root).expanduser() / "observer" / "sets.jsonl"


def load_observer_sets(store_root) -> dict:
    return {r["set_key"]: r for r in read_jsonl(observer_sets_path(store_root))}


def render_observer_sets(records: list, targets: dict, *, store, cfg: ObserverSetConfig,
                         workers: Optional[int] = None, log_every: float = 30.0) -> dict:
    """Plan + render the Observer's training sets for every record; append the new set
    records to observer/sets.jsonl. `targets` = {part_id: [target instances]}.
    Returns a summary with measured throughput."""
    root = _root(store)
    have = load_observer_sets(root)
    done_parts = {r["part_id"] for r in have.values()}
    todo = [r for r in records if r["part_id"] not in done_parts]
    summary = {"parts": len(records), "parts_skipped_done": len(records) - len(todo),
               "sets": 0, "rendered": 0, "cached": 0, "errors": 0}
    t0 = time.time()
    last = t0
    path = observer_sets_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    with RenderPool(workers) as pool:
        summary["workers"] = pool.workers
        jobs = [(r, list(targets.get(r["part_id"], [])), cfg, str(root)) for r in todo]
        for job, res in pool.run(_job_observer, jobs):
            if isinstance(res, BaseException):
                failures.append({"part_id": job[0]["part_id"],
                                 "error": f"{type(res).__name__}: {res}"})
                continue
            fresh = [s for s in res["sets"] if s["set_key"] not in have]
            have.update((s["set_key"], s) for s in fresh)
            summary["sets"] += append_jsonl(path, fresh)
            summary["rendered"] += res["rendered"]
            summary["cached"] += res["cached"]
            failures += [{"part_id": job[0]["part_id"], "error": e} for e in res["errors"]]
            now = time.time()
            if now - last >= log_every:
                last = now
                el = now - t0
                print(f"[observer.render] {summary['rendered']} rendered in {el:.0f}s "
                      f"({summary['rendered'] / max(el, 1e-9):.2f} grids/s)", file=sys.stderr)
    el = time.time() - t0
    summary["errors"] = len(failures)
    summary["seconds"] = round(el, 2)
    summary["grids_per_s"] = round(summary["rendered"] / max(el, 1e-9), 3)
    _log_failures(root, failures)
    return summary


__all__ = ["IMAGE_KEY", "ObserverSetConfig", "RenderError", "RenderPool", "ViewSet",
           "load_observer_sets", "observer_sets_path", "render_key", "render_many",
           "render_observer_sets", "render_sets", "renderer_digest"]
