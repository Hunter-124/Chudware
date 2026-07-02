"""Offline VLM track v0 tests (T-H10). All run in CI — no model, no network, no engine.

They prove the measurement chain is REAL (not all-skip): a mock candidate's claims flow
through the T-H8 bench scoring and produce real precision/recall/residual numbers, and the
"precision is king" contract is enforced end-to-end."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from tools.flywheel.vlm import candidates, corpus_gen, candidate_eval as ce, serving
from tools.flywheel.vlm.recipes import guard, reward


# ---- fixtures ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("vlm_corpus")
    corpus_gen.generate(str(d), seeds=2, images=True)
    return str(d)


@pytest.fixture(scope="module")
def gt_corpus(tmp_path_factory):
    """ground-truth-only corpus (no PIL) — proves the harness scores without images too."""
    d = tmp_path_factory.mktemp("vlm_corpus_gt")
    corpus_gen.generate(str(d), seeds=2, images=False)
    return str(d)


def _perfect(corpus_dir):
    man = json.load(open(os.path.join(corpus_dir, "manifest.json")))
    return {e["example_id"]: [dict(f) for f in e["known_geometry"]]
            for e in man["examples"] if e["kind"] in ("render", "drawing")}


# ---- candidate registry -----------------------------------------------------------------

def test_three_ratified_candidates_all_apache():
    assert set(candidates.CANDIDATES) == {"qwen3vl4b", "granite41v4b", "gemma4e4b"}
    assert candidates.PRIMARY_KEY == "qwen3vl4b"
    for c in candidates.CANDIDATES.values():
        assert c.license == "Apache-2.0"       # bundleable in a paid offline app
        assert c.total_gb > 0

def test_ruled_out_recorded():
    assert "Qwen2.5-VL-3B" in candidates.RULED_OUT   # non-commercial
    assert "Moondream 3" in candidates.RULED_OUT      # BUSL

def test_provision_instructions_have_download_and_serve():
    txt = candidates.provision_instructions("all")
    assert "huggingface-cli download" in txt
    assert "llama-server" in txt and "--mmproj" in txt
    for c in candidates.CANDIDATES.values():
        assert c.gguf_file in txt


# ---- corpus generation ------------------------------------------------------------------

def test_corpus_hash_is_deterministic(tmp_path):
    a = corpus_gen.generate(str(tmp_path / "a"), seeds=3, images=False)
    b = corpus_gen.generate(str(tmp_path / "b"), seeds=3, images=False)
    assert a["hash"] == b["hash"]                # cross-machine reproducibility DoD
    assert a["hash"].startswith("sha256:")

def test_corpus_has_all_three_kinds_and_provenance(corpus):
    man = json.load(open(os.path.join(corpus, "manifest.json")))
    kinds = {e["kind"] for e in man["examples"]}
    assert kinds == {"render", "drawing", "diff_pair"}
    assert all(e["provenance"] == "synthetic" for e in man["examples"])

def test_corpus_images_written_and_readable(corpus):
    from PIL import Image
    man = json.load(open(os.path.join(corpus, "manifest.json")))
    render = next(e for e in man["examples"] if e["kind"] == "render")
    with Image.open(os.path.join(corpus, render["image"])) as im:
        assert im.size == (corpus_gen._IMG, corpus_gen._IMG)

def test_diff_pair_has_known_diff(corpus):
    man = json.load(open(os.path.join(corpus, "manifest.json")))
    pair = next(e for e in man["examples"] if e["kind"] == "diff_pair")
    assert pair["known_diff"]["kind"] == "missing_feature"
    assert len(pair["known_geometry_b"]) < len(pair["known_geometry_a"])

def test_drawing_callouts_are_dimensions(corpus):
    man = json.load(open(os.path.join(corpus, "manifest.json")))
    dwg = next(e for e in man["examples"] if e["kind"] == "drawing")
    assert all(c["kind"] == "dimension" for c in dwg["known_geometry"])


# ---- the measurement chain (REAL numbers, not all-skip) ---------------------------------

def test_perfect_candidate_scores_f1_one(corpus):
    res = ce.evaluate_candidate(ce.MockCandidate("perfect", _perfect(corpus)), corpus)
    assert res.available and res.n_examples > 0
    assert res.mean_f1 == 1.0 and res.mean_precision == 1.0 and res.hallucinations == 0

def test_hallucination_sinks_precision(corpus):
    claims = _perfect(corpus)
    first = next(iter(claims))
    claims[first] = claims[first] + [{"kind": "phantom", "count": 99}]
    res = ce.evaluate_candidate(ce.MockCandidate("h", claims), corpus)
    assert res.hallucinations == 1
    assert res.mean_precision < 1.0          # precision is king — one bad claim drops it
    assert res.pass_rate < 1.0               # that example fails outright

def test_deterministic_ceiling_high_precision_modest_recall(gt_corpus):
    ceil = ce.deterministic_ceiling(gt_corpus)
    assert ceil.mean_precision == 1.0        # only ever restates known facts
    assert ceil.mean_recall < 1.0            # never proposes visible:false internal features

def test_residual_report_verdicts(corpus):
    perfect = _perfect(corpus)
    def factory(spec):
        return ce.MockCandidate(spec.key, perfect) if spec.key == "qwen3vl4b" \
            else ce.LlamaCppVisionCandidate(spec)      # unreachable -> pending
    rep = ce.run_residual(corpus, candidate_factory=factory)
    assert rep["verdicts"]["qwen3vl4b"] == "GO"        # beats ceiling, precision 1.0
    assert rep["verdicts"]["granite41v4b"] == "pending-provision"
    row = next(r for r in rep["candidates"] if r["candidate"] == "qwen3vl4b")
    assert row["residual_over_deterministic"] > 0
    assert "provision" in rep and "huggingface-cli" in rep["provision"]

def test_min_confidence_gate_filters(corpus):
    man = json.load(open(os.path.join(corpus, "manifest.json")))
    # tag every claim 'low' -> a high-confidence floor drops them all -> recall 0
    low = {e["example_id"]: [{**f, "confidence": "low"} for f in e["known_geometry"]]
           for e in man["examples"] if e["kind"] in ("render", "drawing")}
    res = ce.evaluate_candidate(ce.MockCandidate("lo", low), corpus, min_confidence="high")
    assert res.mean_recall == 0.0

def test_unavailable_candidate_is_pending_not_fabricated(corpus):
    down = ce.LlamaCppVisionCandidate(candidates.get("qwen3vl4b"),
                                      base_url="http://127.0.0.1:59999/v1")
    assert down.available is False
    res = ce.evaluate_candidate(down, corpus)
    assert res.available is False and res.mean_f1 == 0.0 and "pending-provision" in res.note


# ---- claim parsing ----------------------------------------------------------------------

def test_parse_claims_tolerates_fences_and_prose():
    text = 'Here is what I see:\n```json\n{"claims": [{"kind": "hole", "count": 4}]}\n```'
    claims = ce.parse_claims(text)
    assert claims == [{"kind": "hole", "count": 4}]

def test_parse_claims_junk_returns_empty():
    assert ce.parse_claims("no json here") == []
    assert ce.parse_claims('{"claims": [{"nope": 1}]}') == []   # no kind -> dropped


# ---- serving seam -----------------------------------------------------------------------

def test_serving_disabled_by_default():
    r = serving.render_self_review("/nonexistent.png", {"experimental": {}})
    assert r.enabled is False and r.ok is False and "disabled" in r.reason

def test_serving_offline_nonloopback_refused():
    cfg = {"offline_mode": True, "experimental": {"vision_review": {
        "enabled": True, "base_url": "http://example.com:1234/v1"}}}
    r = serving.render_self_review("/nonexistent.png", cfg)
    assert r.ok is False and "loopback" in r.reason

def test_serving_loopback_endpoint_down_skips_with_reason(corpus):
    man = json.load(open(os.path.join(corpus, "manifest.json")))
    img = os.path.join(corpus, next(e["image"] for e in man["examples"] if e["kind"] == "render"))
    cfg = {"offline_mode": True, "experimental": {"vision_review": {
        "enabled": True, "base_url": "http://127.0.0.1:59998/v1"}}}
    r = serving.render_self_review(img, cfg)
    assert r.enabled is True and r.ok is False and "unreachable" in r.reason

def test_serving_config_parse_mmproj_fields():
    cfg = {"experimental": {"vision_review": {
        "enabled": True, "base_url": "http://localhost:1234/v1",
        "model": "qwen3-vl-4b", "mmproj": "/m/mmproj.gguf", "model_path": "/m/model.gguf"}}}
    vr = serving.VisionReviewConfig.from_config(cfg)
    assert vr.mmproj == "/m/mmproj.gguf" and vr.model_path == "/m/model.gguf"


# ---- recipes: guard + deterministic reward ----------------------------------------------

def test_training_blocked_by_default(monkeypatch):
    for k in guard._CI_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv(guard.OPT_IN_ENV, raising=False)
    ok, _ = guard.training_allowed()
    assert ok is False
    with pytest.raises(RuntimeError):
        guard.assert_training_allowed()

def test_training_blocked_in_ci_even_with_optin(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(guard.OPT_IN_ENV, "1")
    assert guard.training_allowed()[0] is False       # CI overrides opt-in

def test_reward_grounds_beat_hallucinations():
    facts = [{"kind": "hole", "count": 4}, {"kind": "bbox", "x": 80, "y": 50, "z": 6}]
    grounded = [{"kind": "hole", "count": 4}, {"kind": "bbox", "x": 80, "y": 50, "z": 6}]
    hallucinated = grounded + [{"kind": "phantom", "count": 1}]
    assert reward.render_reward(grounded, facts) > reward.render_reward(hallucinated, facts)
    assert reward.passed_bar(grounded, facts) is True
    assert reward.passed_bar(hallucinated, facts) is False    # precision sinks it

def test_reward_strips_confidence():
    facts = [{"kind": "hole", "count": 4}]
    with_conf = [{"kind": "hole", "count": 4, "confidence": "high"}]
    assert reward.render_reward(with_conf, facts) == 1.0


# ---- DVC round-trip (proves DVC-versioning end-to-end; skips if dvc binary absent) -------

def _dvc_available() -> bool:
    import shutil
    return shutil.which("dvc") is not None

@pytest.mark.skipif(not _dvc_available(), reason="dvc binary not on PATH")
def test_dvc_corpus_roundtrip(tmp_path):
    from tools.flywheel.vlm.dvc_track import roundtrip_demo
    rep = roundtrip_demo(tmp_path / "repo", tmp_path / "remote")
    assert rep["pointer_has_md5"] is True     # .dvc pointer pins the content
    assert rep["bytes_match"] is True         # dvc pull restores identical bytes
