"""Zero-shot candidate residual harness (T-H10 DoD item 1).

Runs a local VLM candidate over the synthetic corpus, collects its COARSE claims about
each image, and scores them THROUGH the T-H8 VLM bench (same `_score_claims`, same
"precision is king" rule — a single hallucination fails an example). The candidate's mean
F1 over the corpus, minus the `deterministic` ceiling, IS the residual that decides GO /
NO-GO for training that candidate (RECOGNITION_RESIDUAL discipline).

Three baseline rows, kept DISTINCT (do not conflate — this was an explicit review note):
  1. `deterministic` — MEASURED here, the describe_scene-style non-vision fallback. The
     floor a candidate must BEAT ("never lose to the deterministic fallback" kill-switch).
  2. the 3 LOCAL candidates — MEASURED when llama.cpp + weights are provisioned; otherwise
     an honest `pending-provision` row with exact commands (NOT a fabricated score). These
     run OFFLINE — they are the residual this task exists to produce.
  3. `frontier` (API) — genuinely pending offline (no creds/network); the per-dollar
     comparator the winner must also beat. Owned by T-H8's `pending_frontier()`.

The whole scoring path is PROVEN offline by `MockCandidate` (deterministic canned claims →
real bench scoring → real precision/recall/residual), so this harness is never all-skip:
`evaluate_candidate(MockCandidate(...), corpus)` returns real numbers with zero weights.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

# Reuse the T-H8 bench scoring verbatim — never re-implement the "precision is king" rule.
from ..bench.benches.vlm import (
    _score_claims, deterministic_claims, RECALL_TARGET,
)
from ..bench.metrics import mean, residual as residual_metric
from .candidates import CANDIDATES, VLMCandidate, get as get_candidate, provision_instructions

# Confidence tiers the VLM tags each claim with (the "confidence-tiered claims JSON"
# intake contract, Driver notes). Scoring is tier-agnostic by default (a claim is a claim),
# but we record the distribution and expose a `min_confidence` gate so a consumer can drop
# low-confidence claims before the deterministic verifier sees them.
CONFIDENCE_TIERS = ("high", "medium", "low")

# The prompt handed to every candidate. Enforces the LOCKED contract: propose COARSE claims,
# NEVER measure (fiducial code measures), emit strict JSON in the bench fact schema.
CLAIM_PROMPT = (
    "You are a CAD render reviewer. Look at the image and list ONLY what you can clearly "
    "see about the part's geometry, as COARSE claims. You are NOT a measuring instrument: "
    "propose kinds and approximate counts, never precise dimensions you cannot read. "
    "Return STRICT JSON: {\"claims\": [{\"kind\": str, \"confidence\": \"high|medium|low\", "
    "...optional attrs like count/diameter/x/y/z...}]}. Omit anything you are unsure of "
    "rather than guessing — an ungrounded claim is worse than a missing one."
)


class Candidate(Protocol):
    """Anything that turns an image into a list of claim dicts (bench fact schema)."""
    name: str
    available: bool
    def propose_claims(self, image_path: str, prompt: str = CLAIM_PROMPT) -> list[dict]: ...


# ---- mock candidate: proves the whole scoring path offline, zero weights ----------------

@dataclass
class MockCandidate:
    """Deterministic canned-claims candidate. Its `claims_by_example` maps example_id ->
    claims; used to PROVE end-to-end scoring offline (real precision/recall/residual with no
    model). Also the substrate for unit tests: feed it a perfect claim set and precision is
    1.0; feed it a hallucinated claim and precision drops — exactly the bench's contract."""
    name: str = "mock"
    claims_by_example: dict[str, list[dict]] = field(default_factory=dict)
    available: bool = True

    def propose_claims(self, image_path: str, prompt: str = CLAIM_PROMPT) -> list[dict]:
        # image_path's basename (sans ext) is the example_id by corpus convention.
        ex = os.path.splitext(os.path.basename(image_path))[0]
        return list(self.claims_by_example.get(ex, []))


# ---- live llama.cpp vision candidate (guarded; runs only when provisioned) --------------

@dataclass
class LlamaCppVisionCandidate:
    """Talks to an OpenAI-compatible loopback vision endpoint (llama-server --mmproj, the
    same seam `serving.py` uses in production). No new pip dependency — stdlib urllib only,
    image sent as a base64 data URI per the OpenAI vision message shape. `available` probes
    the endpoint; when it is down (the CI/offline reality) callers skip-with-reason and the
    candidate is recorded `pending-provision`, never a fake score."""
    spec: VLMCandidate
    base_url: str = "http://127.0.0.1:1234/v1"
    timeout_s: float = 120.0

    @property
    def name(self) -> str:
        return self.spec.key

    @property
    def available(self) -> bool:
        try:
            req = urllib.request.Request(self.base_url.rstrip("/") + "/models")
            with urllib.request.urlopen(req, timeout=3.0) as r:
                return r.status == 200
        except Exception:
            return False

    def propose_claims(self, image_path: str, prompt: str = CLAIM_PROMPT) -> list[dict]:
        import base64
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        body = json.dumps({
            "model": self.spec.gguf_file,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "temperature": 0.0,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = resp["choices"][0]["message"]["content"]
        return parse_claims(text)


def parse_claims(text: str) -> list[dict]:
    """Extract the claims list from a model's free text. Tolerant of markdown fences and
    leading prose — finds the first JSON object with a `claims` array. A model that emits
    junk yields [] (scored as zero recall, never a crash)."""
    import re
    text = text.strip()
    # strip ```json fences
    m = re.search(r"\{.*\"claims\".*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except Exception:
        # last resort: find the innermost balanced object
        try:
            obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except Exception:
            return []
    claims = obj.get("claims", []) if isinstance(obj, dict) else []
    return [c for c in claims if isinstance(c, dict) and c.get("kind")]


# ---- corpus loading (single-image render/drawing examples) ------------------------------

def load_corpus_examples(corpus_dir: str) -> list[dict]:
    """Load the manifest's single-image examples (render + drawing kinds) that carry
    `known_geometry` and an `image` path. Diff-pair examples are scored separately (they
    have two images + a diff fact); this residual is over the render/drawing set that maps
    1:1 onto the T-H8 claims-vs-facts bench."""
    manifest_path = os.path.join(corpus_dir, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    out = []
    for ex in manifest["examples"]:
        if ex.get("kind") in ("render", "drawing") and "image" in ex and "known_geometry" in ex:
            out.append(ex)
    return out


# ---- per-candidate evaluation -----------------------------------------------------------

@dataclass
class CandidateResult:
    name: str
    available: bool
    n_examples: int
    mean_f1: float
    mean_precision: float
    mean_recall: float
    pass_rate: float
    hallucinations: int
    confidence_hist: dict[str, int]
    per_example: list[dict]
    note: str = ""

    def as_row(self) -> dict:
        return {"candidate": self.name, "available": self.available,
                "mean_f1": round(self.mean_f1, 4), "precision": round(self.mean_precision, 4),
                "recall": round(self.mean_recall, 4), "pass_rate": round(self.pass_rate, 4),
                "hallucinations": self.hallucinations, "n": self.n_examples,
                "note": self.note}


def evaluate_candidate(candidate: Candidate, corpus_dir: str,
                       *, min_confidence: Optional[str] = None) -> CandidateResult:
    """Run `candidate` over the corpus and score its claims through the T-H8 bench.
    `min_confidence` (optional) drops claims below a tier before scoring — a consumer knob
    for trading recall against precision. Returns real numbers for any candidate whose
    `propose_claims` returns claims (the mock proves this offline); an UNAVAILABLE candidate
    short-circuits to a pending result with instructions, never a fabricated score."""
    if not getattr(candidate, "available", True):
        name = getattr(candidate, "name", "candidate")
        return CandidateResult(name=name, available=False, n_examples=0, mean_f1=0.0,
                               mean_precision=0.0, mean_recall=0.0, pass_rate=0.0,
                               hallucinations=0, confidence_hist={}, per_example=[],
                               note="pending-provision (endpoint unreachable); see provision_instructions()")

    examples = load_corpus_examples(corpus_dir)
    tier_rank = {t: i for i, t in enumerate(reversed(CONFIDENCE_TIERS))}  # high=2..low=0
    conf_hist = {t: 0 for t in CONFIDENCE_TIERS}
    per_example, f1s, precs, recs, passes, halluc = [], [], [], [], 0, 0

    for ex in examples:
        facts = ex["known_geometry"]
        image_path = os.path.join(corpus_dir, ex["image"])
        claims = candidate.propose_claims(image_path)
        for c in claims:
            conf_hist[c.get("confidence", "high") if c.get("confidence") in CONFIDENCE_TIERS
                      else "high"] += 1
        if min_confidence is not None:
            floor = tier_rank[min_confidence]
            claims = [c for c in claims
                      if tier_rank.get(c.get("confidence", "high"), 2) >= floor]
        # strip confidence before matching — it's an intake-protocol attr, not a geometry fact
        scored_claims = [{k: v for k, v in c.items() if k != "confidence"} for c in claims]
        s = _score_claims(scored_claims, facts)
        passed = bool(s["recall"] >= RECALL_TARGET and s["precision"] == 1.0)
        f1s.append(s["f1"]); precs.append(s["precision"]); recs.append(s["recall"])
        passes += int(passed); halluc += s["hallucinations"]
        per_example.append({"example_id": ex["example_id"], "structure": ex.get("structure"),
                            "kind": ex.get("kind"), "f1": round(s["f1"], 4),
                            "precision": s["precision"], "recall": round(s["recall"], 4),
                            "hallucinations": s["hallucinations"], "passed": passed})

    n = len(examples)
    return CandidateResult(
        name=getattr(candidate, "name", "candidate"), available=True, n_examples=n,
        mean_f1=mean(f1s), mean_precision=mean(precs), mean_recall=mean(recs),
        pass_rate=(passes / n if n else 0.0), hallucinations=halluc,
        confidence_hist=conf_hist, per_example=per_example)


def deterministic_ceiling(corpus_dir: str) -> CandidateResult:
    """The MEASURED describe_scene-style floor over the SAME corpus — the ceiling every
    candidate must beat. Built by feeding each example's own `deterministic_claims()`
    through the identical scoring path, so the residual is apples-to-apples."""
    examples = load_corpus_examples(corpus_dir)

    class _Det:
        name = "deterministic"
        available = True
        def propose_claims(self, image_path, prompt=CLAIM_PROMPT):
            ex = os.path.splitext(os.path.basename(image_path))[0]
            fx = next((e for e in examples if e["example_id"] == ex), None)
            return deterministic_claims(fx["known_geometry"]) if fx else []

    return evaluate_candidate(_Det(), corpus_dir)


# ---- residual report + go/no-go verdict -------------------------------------------------

def run_residual(corpus_dir: str, *, candidate_keys: Optional[list[str]] = None,
                 base_url: str = "http://127.0.0.1:1234/v1",
                 candidate_factory: Optional[Callable[[VLMCandidate], Candidate]] = None,
                 ) -> dict[str, Any]:
    """Full zero-shot residual pass: measure the deterministic ceiling, then every requested
    candidate, then emit GO/NO-GO. `candidate_factory` lets tests inject mock candidates;
    in production each candidate is a `LlamaCppVisionCandidate` against the loopback endpoint
    (unreachable offline -> pending-provision row). Never fabricates a score."""
    keys = candidate_keys or list(CANDIDATES)
    factory = candidate_factory or (lambda spec: LlamaCppVisionCandidate(spec, base_url=base_url))

    ceiling = deterministic_ceiling(corpus_dir)
    det_mean = ceiling.mean_f1

    rows: list[dict] = []
    verdicts: dict[str, str] = {}
    for k in keys:
        spec = get_candidate(k)
        cand = factory(spec)
        res = evaluate_candidate(cand, corpus_dir)
        row = res.as_row()
        if res.available:
            r = residual_metric(res.mean_f1, det_mean)
            row["residual_over_deterministic"] = round(r["residual"], 4)
            # kill-switch condition 1: never lose to the deterministic fallback.
            beats_floor = res.mean_precision >= 1.0 and res.mean_f1 > det_mean
            # condition 2 (frontier-per-dollar) is PENDING offline — recorded, not decided.
            verdicts[k] = "GO" if beats_floor else "NO-GO"
            row["verdict"] = verdicts[k]
            row["frontier_per_dollar"] = "pending (no API offline)"
        else:
            row["residual_over_deterministic"] = None
            verdicts[k] = "pending-provision"
            row["verdict"] = "pending-provision"
        rows.append(row)

    return {
        "corpus_dir": os.path.relpath(corpus_dir),
        "deterministic_ceiling": ceiling.as_row(),
        "candidates": rows,
        "verdicts": verdicts,
        "kill_switch": {
            "cond1_beat_deterministic": "measured (mean_f1 > deterministic AND precision==1.0)",
            "cond2_beat_frontier_per_dollar": "pending — needs the API frontier row (T-H8) + $/call",
        },
        "recall_target": RECALL_TARGET,
        "provision": provision_instructions("all"),
        "generated": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
    }
