# T-H7 Questions

## Q1: Double-emit reconciliation for IP-1 (loop.py already emits ai_artifact)

**Context:** `services/agent/loop.py` (T-H1) already calls `_emit_ai_artifact` per
successful build iteration at IP-1, enriched with `verifier_scores` from the
post-build verifier pass. If the `LoggingProvider` wrapper also emits `ai_artifact`
on every `complete_ex` call, IP-1 produces two events per iteration — one at
generate-time (no scores yet) and one at build-time (full scores, correct).

**Recommended answer:** Suppress IP-1 emission from the wrapper. The wrapper emits
only for insertion points that lack a downstream build-and-score loop (IP-12, IP-18,
IP-20 and any future ones). loop.py's post-build emission is authoritative for IP-1
because it is the only site where `verifier_scores` are available. The wrapper's job
at IP-1 is to write the request/response sidecar paths and return them via its return
annotation so loop.py can pass them as `input_ref`/`output_ref`.

**Alternative A:** Wrapper emits universally, loop.py emission removed, scores
attached via a follow-up `ai_artifact_action` event. Weak — `ai_artifact_action`
carries edit ops, not verifier scores; requires changing T-H1 contract.

**Alternative B:** Emit both; consumers deduplicate on `artifact_id`. Fragile;
artifact_id is keyed on (run_id, iteration_index) so it's stable, but two events
with the same id and different scores will confuse the exporter.

**Recommended implementation:** `LoggingProvider` accepts an optional
`insertion_point: str = "IP-1"` ctor arg AND a `suppress_emit: bool = False` arg.
When `suppress_emit=True` the wrapper still writes sidecars and returns sidecar
paths (via a thread-local or a context-var stash readable by the caller) but does not
call `_emit_ai_artifact`. The loop.py call site then reads the sidecar paths and
passes them to its existing emission. This way DoD#1 is met without double-emit and
without modifying T-H1 loop emission.

**Proceeding unblocked:** building the shadow harness (replay reads the log, not the
emit site) and the non-IP-1 emission path now. IP-1 sidecar-passing wiring is the
only blocked sub-item; it is marked [~] in DoD pending the answer.

---

ANSWER (Fable):
