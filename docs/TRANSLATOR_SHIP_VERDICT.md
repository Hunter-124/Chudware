# Translator ship verdict (T-F11 v0)

GENERATED companion to the slot-fidelity baseline in `docs/RECOGNITION_RESIDUAL.md`
(`python3 -m tools.translator.eval.harness --append-residual`). Records the Pillar-13 ship
gates' current state and the **kill-switch verdict**. Re-evaluate after provisioning a model.

## Verdict: **HOLD — no ship (pending the downstream measurement)**

A documented no-ship/pending verdict is a valid task completion (T-F11 DoD). No learned
translator is provisioned offline, so the kill-switch WIN axis is unmeasured. The
deterministic template (`gsl_ser.render_caption`) remains the shipping narrator; the learned
translator ships only when it clears the gate below.

## Ship-gate state

| gate | threshold | state (v0) |
|---|---|---|
| numeric fidelity | ≥ 99.5% | **template = 1.00 by construction (ceiling)**; candidate = pending-provision |
| unsupported-claim rate | < 0.1% on held-out **structures** | template = 0.0; candidate = pending |
| slot coverage | ≥ 98% | template = 1.00; candidate = pending |
| round-trip re-extraction | exact | **PASS** (all gold states) |
| scorer self-check | template ~1.0, ROC-AUC 1.0 | **PASS** (validates the metric, not a model) |
| **kill-switch** | beat GSL templates on **downstream task success**, never lose on fidelity | **PENDING** — see measurement below |

**Why fidelity is not the deciding gate (the inversion):** `render_caption` is generated
*from* the ground-truth slots, so it scores ~1.0 fidelity by construction. A learned model
can at best TIE it on fidelity — fidelity/coverage/unsupported are ABSOLUTE safety gates
(and a non-regression bar), NOT a residual to beat. The model earns its place only on the
kill-switch WIN axis, which fidelity cannot show.

## What ships TODAY regardless (safety)

The inference-time **output gate** (`tools/translator/gate.py`, verified behaviorally): any
ungrounded or wrong numeral in a model caption → discard it, emit the deterministic template.
So enabling a local translator can never regress fidelity below the template — the worst
case is "we fell back to templates." This is shippable now, before any training.

## The pending measurement (run this post-provisioning to resolve the kill-switch)

The kill-switch compares **downstream agent task success**, template-narration vs
model-narration — a different metric than slot fidelity, and the only one that justifies the
model. It is unmeasurable offline (needs a served, SFT'd model). Exact spec for Erik/foreman:

1. **Provision + SFT** a candidate (out-of-band, never CI):
   `python -c "from tools.translator.candidates import provision_instructions as p; print(p('all'))"`
   then run `tools/translator/recipes/sft_qlora.yaml` with `AICAD_ALLOW_TRANSLATOR_TRAINING=1`.
2. **Fidelity gate first** (must pass before the kill-switch even runs):
   `python -m tools.translator.candidate_eval` against a served endpoint → require
   fidelity ≥ 0.995, coverage ≥ 0.98, unsupported < 0.001 on held-out **structures**, and
   fidelity ≥ template (non-regression). Winner picked by `pick_winner`.
3. **Downstream-task harness** (the WIN axis): pick a fixed set of N real agent tasks whose
   success is deterministically checkable (e.g. the T-A2 node-runner over a DAG, scored by
   `eval_compare` match_pct / verifier-battery pass, per the T-H8 CAM-bench precedent). Run
   each task **twice** — once with narration = `render_caption` (control), once with
   narration = the gated model (`TranslatorNarrator`) — same seeds, same tasks.
   - **SHIP iff** model-narration task-success > template task-success (strictly) AND
     model fidelity is not below template (never lose on fidelity).
   - **NO-SHIP** otherwise — templates are free; keep the deterministic narrator.
4. Record the two success rates + the decision here and re-run
   `--append-residual`. Until then: **HOLD**.

## Known limitation — the scorer under-credits natural prose (T-F10 coordination)

**Verified:** a fully-CORRECT natural-prose caption (e.g. *"This bolt plate measures 120 mm
by 80 mm and is 10 mm thick…"*, every number right) is scored **fidelity 0.43, 6 missing, 2
false-`wrong`** and **REJECTED** by the gate → fallback to template. Cause: the FROZEN T-F10
`slot_accuracy._select` aligns caption numerals to slots by keyword/path proximity, so a
caption without the `part.bbox.x`-style path tokens mis-aligns its (correct) numbers.

**Why this matters:** the kill-switch WIN axis is *more natural narration* — but the current
scorer penalizes exactly that. Two consequences:
1. **v0 is unaffected:** the SFT target is `render_caption` (path-labeled prose), so a
   distilled model emits path-labeled captions that DO align and pass the gate. The
   limitation bites only if the translator is pushed toward free natural prose.
2. **The downstream kill-switch measurement (above) needs a fidelity check that does not
   punish natural phrasing** — otherwise a genuinely-better natural narrator reads as a
   fidelity regression. Options (T-F10's call, not fixable here — frozen contract):
   (a) keep path-labeled captions as the SFT target (safest; narration stays structured);
   (b) improve `_select` to map domain synonyms ("wide"→bbox.x) before free-prose training;
   (c) score free prose with the groundedness checker's value-membership only (no slot
   alignment) for the WIN-axis eval. **Filed as a coordination note for T-F10/downstream.**
   Pinned by `test_natural_prose_currently_undercredited_KNOWN_LIMITATION`.

## Scope note

T-F11 v0 delivered: the output gate (shippable), the deterministic GRPO reward, the
candidate-eval + winner-pick harness (mock-proven), the SFT/GRPO recipe scaffold (CI-guarded),
the serving narrator seam + GBNF grammar, and this verdict. NO training ran (scope fence).
