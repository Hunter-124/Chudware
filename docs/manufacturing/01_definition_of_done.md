# Manufacturing Overhaul Definition of Done v4

This revision supersedes every earlier amendment.

## Product choices
- Runtime approval is selectable per job: `human_only` or `human_or_agent`, default `human_only`, bounded by commissioning policy. The arbiter enforces it and logs initiator provenance and nonce burn.
- Expert direct-Grbl mode for the Shapeoko pilot, explicitly outside Carbide's documented Carbide Motion workflow unless approved by Carbide.
- Feedback-capable relay/VFD is required for automated spindle authority; otherwise motion can run only after explicit manual spindle confirmation.
- Attendance is policy by machine class. Open/hobby machines require physical attendance; only separately qualified enclosed/interlocked machines may allow remote-attended/unattended policies.
- Fixed multi-camera first; Shapeoko plus generic probes; no physical printer target until virtual qualification; 3D printing is the first predictive-model dataset.
- Publish the MLDP model/training core; user data stays local by default with explicit opt-in export.
- PrusaSlicer fork remains an out-of-process AGPL sidecar with exact corresponding-source artifacts.

## Acceptance criteria
1. `scripts/gate.sh` green. SKIPs only for documented absent prerequisites: OCCT, runtime venv, emsdk, `CHUDWARE_SLICER_BIN`, or an optional twin binary.
2. Every device reports explicit truth levels: `plan`, `generate`, `post`, `parse`, `simulate`, `monitor`, `transport`, `execute`, `safety_integration`. `assumed` disables actions exactly like absent and names the missing level.
3. No verdict word without its complete check set. Verdict is computed, revision/artifact bound, flips unknown on edits, and Live mode reads the frozen approval binding rather than editable planning state.
4. Sole external freshness shape is `FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}`. `v` is null unless `state==ok`; last-good is display-only. Sequence gaps force unknown. Twin remains tick-domain internally; publisher stamps the external envelope.
5. Two-stage gates. Stage 1 admits only fresh `state==ok && src==probed` evidence (and verified execute capability); it is necessary, never sufficient. Unknown/stale/refused and advisory/simulated/assumed/declared fail closed. `unsupported` may become `not_applicable` only when independently corroborated for the same subject/config epoch by fresh evidence.
6. Tier 0 construction refusal handles action prerequisites before a proposal or preflight exists and renders the missing prerequisite at the action entry point. Classification is artifact-dependent. A guard catching a prerequisite is defense-in-depth, never a substitute. Missing evidence fails; it is never not-applicable.
7. Stage 2 is `pass|fail|not_applicable`; no unknown. Not-applicable is only a missing corroborating sub-condition, never a whole action prerequisite, and never counts as pass. Machine-class irrelevant checks are omitted from the required set. Named waivers are durable `{by,reason,at_seq}` and aggregate waiver rates are visible.
8. Guard/gate liveness: each producer guard can pass for some producer output and can fail for some input; approval-path owners drive every gate to pass from at least one source set; runtime reports gates waived on effectively every recent run. Deliberately absent guards and exemptions are documented.
9. Invalidation envelope is `{class:"falsified"|"superseded",reason,why,affected:[ids],at_seq,superseded_by?}`. Falsification clears; supersession retains and points forward without alarm. Every result carries establishing durable `seq`; apply notices only to rows with `seq<=at_seq`. Producers push invalidations; they never wait for polling/staleness.
10. Gateway owns durable seq stamping and lane-epoch/process-start reconciliation for every durable result a UI may paint, not only telemetry. Corroborators must be independently fresh and subject/config-bound.
11. Approval binds machine identity/fingerprint, immutable artifact hash, job/setup revision, tool/fixture/WCS, preflight evidence, approver, expiry, `exercisable_by`, one-shot nonce, and `max_starts=1`. Fingerprint or bound-input changes falsify approval. Initiator and approval id remain visible.
12. Approved-job autonomy: AI may request start only with an active human approval permitting agent exercise; arbiter revalidates, starts, burns nonce, logs. AI may request hold/abort. It may never approve itself, resume after alarm/reset/disconnect, jog, home, zero, override, unlock, bypass interlocks, or issue safety commands.
13. No transparent resume after reset, alarm, hard limit, disconnect, or power loss. Only a new authored run with new inspection/preflight/approval may follow.
14. Simulation and perception never satisfy execute-side gates. Simulated conclusions are context only; Gateway may gate on procedural facts that a named check ran against a specific artifact/config/snapshot/seq.
15. One command owner per device. Approval validation lives only in the deterministic arbiter, never in MCP guards or model prompts. Motion/safety risk is explicit-membership; safety actions have no model allow path.
16. Public `libchudware` C ABI carries no `mfg_*` symbols. Gateway/device execution remains a separate service/process boundary.
17. No GPL/AGPL/non-commercial code, weights, or datasets are linked or copied into the permissively licensed core. Fast gate rejects in-process libslic3r/CuraEngine coupling. Every distributed slicer sidecar has matching source, hashes, build scripts, notices, and a live source URL.
18. Emulator progression is capability-gated: hardware-free E0-E3 in normal gates; pilot E6 before `execute` is advertised. No capability level may exceed the highest emulator/hardware stage actually passed.
19. Tier-0 registers, guard source tables, prerequisite/corroborating/artifact-dependent columns, invalidation events, and durable schemas are contract-tested as policy boundaries.
20. Major waves record their change summary and update the relevant developer documentation in the same green commit.
