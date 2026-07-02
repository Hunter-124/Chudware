"""T-F11 translator track tests. All run in CI — no model, no network, no engine.

Prove the safety-critical piece behaviorally (not just asserts): a hallucinated caption is
REJECTED by the gate and narration falls back to the deterministic template; a grounded
caption passes. Plus the multi-term reward, candidate-eval framing, and the CI training guard.
"""
from __future__ import annotations

import glob
import json
import os

import pytest

from tools.translator import gate, reward, serving, candidate_eval as ce, candidates
from tools.translator.recipes import guard
from tools.translator.gsl_ser import render_caption
from tools.translator.groundedness import H4GroundednessAdapter, is_stub, get_checker


@pytest.fixture(scope="module")
def state():
    gp = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "eval", "gold", "*.json")))[0]
    return json.load(open(gp))["state"]


# ---- the output gate (safety-critical, behaviorally verified) ---------------------------

def test_grounded_caption_passes(state):
    text, src, g = gate.narrate(state, render_caption(state))
    assert src == "model" and g.passed and g.extrinsic == 0 and g.wrong == 0

def test_ungrounded_numeral_falls_back_to_template(state):
    hallu = render_caption(state) + " The secret bore radius is 999.9 mm."
    text, src, g = gate.narrate(state, hallu)
    assert src == "template" and not g.passed and g.extrinsic >= 1
    assert text == render_caption(state)          # never fiction — deterministic fallback

def test_wrong_value_falls_back_to_template(state):
    tmpl = render_caption(state)
    # corrupt a real integer slot value that appears in the template
    import re
    m = re.search(r"is (\d+)mm", tmpl)
    assert m, "expected a mm slot in the template"
    wrong = tmpl.replace(m.group(0), f"is {int(m.group(1)) + 500}mm", 1)
    text, src, g = gate.narrate(state, wrong)
    assert src == "template" and not g.passed and g.wrong >= 1

def test_absent_model_uses_template(state):
    text, src, g = gate.narrate(state, None)
    assert src == "template" and text == render_caption(state)

def test_gate_uses_explicit_checker_not_global_stub(state):
    # gate must not mutate the module default — it stays the stub after we use the adapter
    gate.check_caption(render_caption(state), state, checker=H4GroundednessAdapter())
    assert is_stub() is True and type(get_checker()).__name__ == "_MembershipStub"


# ---- narrator serving seam --------------------------------------------------------------

def test_narrator_template_fallback_on_model_error(state):
    def boom(prompt): raise RuntimeError("server down")
    text, src, g = serving.TranslatorNarrator(complete_fn=boom).narrate(state)
    assert src == "template"                       # serving error -> safe template

def test_narrator_passes_grounded_model_output(state):
    # a model that echoes the template is fully grounded -> emitted
    n = serving.TranslatorNarrator(complete_fn=lambda prompt: render_caption(state))
    text, src, g = n.narrate(state)
    assert src == "model" and g.passed

def test_build_prompt_has_ser_header(state):
    p = serving.build_prompt(state, task="describe only what changed")
    assert "gsl-ser/v1" in p and "describe only what changed" in p


# ---- deterministic multi-term reward ----------------------------------------------------

def test_reward_penalizes_hallucination(state):
    grounded = render_caption(state)
    hallu = grounded + " Phantom torque is 88.8 N."
    assert reward.translator_reward(grounded, state) > reward.translator_reward(hallu, state)

def test_reward_blocks_omission_hacking(state):
    """A near-empty caption has perfect groundedness but low coverage -> lower reward than
    the full grounded caption (the multi-term point)."""
    full = render_caption(state)
    empty = "[gsl-ser/v1 domain=brep] part.name is x."
    assert reward.translator_reward(full, state) > reward.translator_reward(empty, state)

def test_reward_breakdown_terms(state):
    b = reward.reward_breakdown(render_caption(state), state)
    assert b["groundedness"] == 1.0 and b["wrong"] == 0 and b["extrinsic"] == 0


# ---- candidate eval + winner pick -------------------------------------------------------

def test_echo_template_candidate_passes_gates():
    r = ce.evaluate_candidate("echo", lambda s: render_caption(s))
    assert r.available and r.passes_gates and r.fidelity == 1.0 and r.unsupported_rate == 0.0

def test_hallucinating_candidate_fails_gates():
    r = ce.evaluate_candidate("hallu", lambda s: render_caption(s) + " Ghost dim is 42.4 mm.")
    assert r.available and not r.passes_gates and r.unsupported_rate > 0

def test_absent_candidate_is_pending_not_fabricated():
    r = ce.evaluate_candidate("qwen3_4b", None)
    assert r.available is False and "pending-provision" in r.note

def test_pick_winner_pending_when_none_available():
    v = ce.pick_winner([ce.evaluate_candidate("qwen3_4b", None),
                        ce.evaluate_candidate("gemma3_4b", None)])
    assert v["winner"] is None and v["status"] == "pending-provision"


# ---- candidate registry -----------------------------------------------------------------

def test_two_ratified_candidates():
    assert set(candidates.CANDIDATES) == {"qwen3_4b", "gemma3_4b"}
    assert candidates.PRIMARY_KEY == "qwen3_4b"
    assert candidates.get("qwen3_4b").license == "Apache-2.0"

def test_ruled_out_recorded():
    assert "Qwen2.5-3B" in candidates.RULED_OUT and "DiffusionGemma" in candidates.RULED_OUT

def test_provision_instructions_serve_gguf():
    txt = candidates.provision_instructions("all")
    assert "llama-server" in txt and "Q4_K_M" in txt


# ---- training guard ---------------------------------------------------------------------

def test_training_blocked_by_default(monkeypatch):
    for k in guard._CI_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv(guard.OPT_IN_ENV, raising=False)
    assert guard.training_allowed()[0] is False
    with pytest.raises(RuntimeError):
        guard.assert_training_allowed()

def test_training_blocked_in_ci_even_with_optin(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(guard.OPT_IN_ENV, "1")
    assert guard.training_allowed()[0] is False
