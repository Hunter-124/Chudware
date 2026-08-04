# T-H9 — questions / seam rulings (opus-flint)
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

Both are NON-BLOCKING — I implemented the recommended answers and left clean seams. Flagging
for foreman visibility + so no one double-implements.

## Q1 — Settings "which brain answered": wire the C++ panel now, or expose data + defer?

**Context.** DoD asks for Settings visibility of the active adapter/version. The read-only
DATA is done and testable: MCP `serving_status()` returns per-IP
`{provider, model, adapter_version, canary_pct, fallback, history}`. The *C++ panel* that would
render it (`ui/imgui/.../settings_modal.cpp` + the legacy `host/src/ui/settings_modal.cpp`)
already has a "Model routing" section, and the survey found host/ vs ui/imgui copies of
`settings_modal.cpp`/`model_registry.h` have **diverged** and are actively churned by the
G-wave (G3 branding, G6 chrome, G12 walkthrough).

**Recommended answer (implemented): expose data, defer the C++ line.** Per the first-claimer /
hot-spot rule, T-H9 ships the data seam (`serving_status`) and does NOT edit the contested
diverged settings panel. The G-wave owner adds the one read-only line binding the panel to
`serving_status()` (or its C++ mirror) when the settings-panel divergence is resolved.
*Alternative:* have T-H9 patch only the `ui/imgui` copy now — rejected: it would collide with
live G3/G6 edits and the legacy copy would drift further.

## Q2 — Live loop.py per-insertion-point routing: T-H9 or a loop-integration task?

**Context.** T-H9 delivers the routing DECISION layer (`RoutingTable.resolve → RouteDecision`)
and the telemetry write-point (`RouteDecision.telemetry_tag()` → `ai_artifact.model.adapter`,
whose slot both `logging_provider.py:274` and `core/agent/run.cpp:836` already reserve). But the
survey confirms `services/agent/loop.py` today constructs ONE provider via `get_provider()` and
does NOT route per insertion point — and even T-H7's `LoggingProvider.wrap()` is not yet wired
into `loop.py` live (only exercised in tests). So "the live loop emits the adapter tag on every
canary response" needs a call site that (a) resolves the route per insertion point and (b)
threads `telemetry_tag()` into the emit.

**Recommended answer (implemented as a seam): land the routing + tag logic in T-H9 (done, CI-
proven); the live loop wiring is a small follow-up on the loop-integration surface** (natural
fit: T-F17 GUI action bridge / a loop-routing task), because it co-edits `loop.py`'s provider
construction + emit, which is a separate hot-spot from the serving layer. T-H9's public seam for
that wiring: `services.serving.routing.load_routing_table().resolve(ip, user_id=...)` →
`get_provider("vllm", cfg_with_adapter)` for the call, then splice `decision.telemetry_tag()`
into the existing `_emit_ai_artifact` `model` block. *Alternative:* T-H9 edits loop.py now —
rejected: loop.py has no insertion-point routing scaffold yet and it overlaps T-F17's agent
bridge; adding it here would be a speculative edit to a hot file ahead of its consumer.

## Q3 — Is a model-suppliable `approver`/`report_ref` a strong-enough sign-off? (foreman ruling)

**Context.** `serving_promote` takes `approver` + `report_ref` as plain MCP tool args and
enforces: report file must EXIST, evidence must be eligible, sign-off must be non-empty. It does
NOT verify the report's content or that Erik personally signed it — and a model can call this
tool. So the "human trigger, no auto-promote" guarantee is only as strong as *who invokes the
tool*. This is consistent with the other human-gated tools here (ask_user, quote-out) and is a
reasonable v1 seam, but promotion is the Erik-RATIFIED safety centerpiece.

**Recommended answer (needs foreman/Erik ruling): acceptable for v1 as-is, with a hardening
follow-up.** Options if you want it stronger: (a) route the sign-off through the `ask_user` /
interaction seam (T-F4) so the human is prompted in-app rather than a model passing a string;
and/or (b) require `report_ref` to contain a signed token (e.g. an HMAC / a line Erik writes) that
`promote` validates, not just `os.path.exists`. Both are additive to the current gate. I left the
existing behavior (existence + eligibility + non-empty sign-off) and flag this for your call —
non-blocking, no auto-promote path exists either way.

## ANSWER (Q3) — Fable advisor (delegated by Erik), 2026-07-03

**Keep v1 as-is; hardening deferred.** Erik's direction (2026-07-03): no model
fine-tuning/serving for a while — the program's core functionality, AI harness, and GUI
come first. Since nothing will be promoted to production serving in the near term, the
plain-string gate is acceptable; there is no auto-promote path and no live serving box.
REVISIT CONDITION: before the first real adapter promotion, route the sign-off through
the in-app `ask_user` interaction seam (option a) — record that as a DoD item on
whatever task stands up real serving.

## ANSWER (Q2) — Fable advisor, 2026-07-03

Seam-now/wire-later ratified (it merged that way). Verified 2026-07-03 that
`services/agent/loop.py` still has zero references to `RoutingTable` /
`telemetry_tag` — T-F17 merged without wiring it, so the canary/telemetry story is
currently inert. The live wiring is now filed as **T-BL22**
(`work/backlog/T-BL22.md`), together with the equally-dormant T-H11 regen_diagnose
GUI binding (same loop/copilot surface, one task). Q3 (promotion-gate strength) is
Erik's call — batched below.

## Batched for Erik (human-only)

- **Live-gated DoD** (needs a real GPU + vLLM; cannot run in CI): two adapters served from one
  vLLM instance, canary flip live-without-restart against real serving, two-OS reachability.
  Exact run commands are in `docs/serving_runbook.md` §1/§3/§7. All the *logic* (routing, canary
  bucketing, rollback, fallback, promotion gate) is CI-tested (48 tests).
- **Promotion sign-off** is your manual gate per the ratified criteria — `serving_promote`
  requires your `approver` + a `report_ref` to a signed-off `serving_evaluate` report; there is
  no auto-promote path.
