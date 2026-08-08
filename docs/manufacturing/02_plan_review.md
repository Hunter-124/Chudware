# Manufacturing Master-Plan Consistency Review

Reviewer: `ArchitectureConsistencyReviewer`. Design-only: no repository file was edited, and no git,
build, gate, linter, formatter or test command was run to produce this document.

Corpus read in full: `local://manufacturing-dod-v4.md` (authoritative), `local://manufacturing-gateway-final.md`
(1490 lines incl. §§24.7-24.11), `local://manufacturing-twin-final.md`, `local://manufacturing-monitoring-architecture.md`,
`local://manufacturing-cutting-process-architecture.md`, `local://manufacturing-turning-architecture.md`,
`local://manufacturing-pnp-architecture.md`, `local://manufacturing-robot-architecture.md`,
`local://additive-architecture.md`, `local://metrology-architecture.md`, `local://perception-architecture.md`,
`local://manufacturing-decisions.md`, and `agent://ManufactureUxArchitecture`.

Nothing below invents product scope or new rules. Every required correction is a reconciliation of two or
more statements already inside the accepted corpus, or of one statement against DoD v4.

Note on `agent://ManufactureUxArchitecture`: the artifact is a **freeze summary, not the design**. It
enumerates deliverables (6 surfaces, 15 new files under `host/src/ui/manufacture/**`, 12 named existing
files, a 2-member `app_state.h` append, `FreshValue<T>`, 9 waves, 12 non-goals) but the schemas,
disabled-state copy rules and the Tier-0 register it says are frozen are **not in the artifact text**.
Every peer binds to UX for `FreshValue<T>` and for the run-approval row set (additive §8.2.1(ii) assigns
UX bullet-(ii) unsatisfiability audit; metrology §"Guard audit"(ii) does the same). That set is not
readable anywhere in the corpus. See **B16**.

---

## Verdict summary

| # | Finding | Class | Sev |
|---|---|---|---|
| B1 | Stage 1 has no `procedural_fact` admission class; four domains' only gate is unfireable | BLOCK | P0 |
| B2 | Corroboration rule is self-contradictory and its contract test asserts the opposite | BLOCK | P0 |
| B3 | The hardware-free E0-E3 gate cannot drive any approval-path gate to pass | BLOCK | P0 |
| B4 | Printer `execute` advertisement specified three incompatible ways | BLOCK | P1 |
| B5 | Provenance laundering: the virtual-printer fixture types as `probed` | BLOCK | P0 |
| B6 | Additive claims `src:"probed"` for a computed preflight report | BLOCK | P1 |
| B7 | Additive and metrology re-declare `CapabilityTruth` with `probed` as a truth value | BLOCK | P1 |
| B8 | Additive mints its own durable `seq` | BLOCK | P1 |
| B9 | Metrology computes its own invalidation closure and keeps a second durable store | BLOCK | P1 |
| B10 | Four domains' durable-result kinds are absent from the W1.1 table → refused at append | BLOCK | P0 |
| B11 | The `manufacture`-family-off-by-default mechanism has no owner and no wave | BLOCK | P0 |
| B12 | Perception registers a second MCP family; additive and metrology name none | BLOCK | P1 |
| B13 | `contracts/worker_rpc.md` §7 claimed twice; `contracts/events.md` §5 amendment unowned | BLOCK | P1 |
| B14 | `services/mcp/tool_registry.py` has five claimants in overlapping waves | BLOCK | P1 |
| B15 | `core/cam/post.cpp:487` requires a joint edit by two owners | BLOCK | P1 |
| B16 | The UX slice is frozen-by-summary; its schemas and gate rows are not in the corpus | BLOCK | P1 |
| C1 | Additive path/prefix divergence from Main's rulings; §0.10 reservation table incomplete | CORRECT | P2 |
| C2 | DoD-16 ABI gate is lexical; additive's `print_*` C ABI surface bypasses it by construction | CORRECT | P2 |
| C3 | Invalidation event dispatch specified five different ways | CORRECT | P2 |
| C4 | Monitoring §11.2 still says E1/E2 are gate-skippable | CORRECT | P2 |
| C5 | Twin §11 and §14 contradict each other on the twin-binary SKIP | CORRECT | P2 |
| C6 | Twin's prohibited-word grep fails against twin's own corpus | CORRECT | P2 |
| C7 | `emulated` machine class claims `safety_integration: not_applicable` uncorroborated | CORRECT | P2 |
| C8 | Capability rows: FreshValue-wrapped (3 domains) vs never-wrapped (PnP) | CORRECT | P2 |
| C9 | Turning's fixtures diverge from the ratified `fixtures/mfg/**` root | CORRECT | P3 |
| C10 | Cross-reference drift on the two most load-bearing §24 rules | CORRECT | P3 |
| P1..P9 | Confirmed-clean properties (see §PASS) | PASS | — |

**Go/no-go: NO-GO for W1.1 as the corpus stands.** Sixteen blocking items, five of them P0. Ten are
one-paragraph reconciliations inside a single document; six (B10-B16) require a named owner and a wave
slot that no document currently assigns. None requires new product scope. See §Go/no-go for the exact
gating set and the smallest edit that turns this to GO.

---

## BLOCK findings

### B1 — Stage 1 admits only `probed`, but four domains' only gate is built on `procedural_fact` (P0)

**Evidence.** Gateway §10.1: Stage 1 "Admits **only** `state=="ok" && src=="probed"` (and, for
execute-side subjects, verified `execute` capability). Rejects `unknown|stale|refused|absent` and
`advisory|simulated|assumed|declared`." No `procedural_fact` admission class appears anywhere in §10
(grep of gateway §§10.1-11 finds `admission` only in the §10.4 heading and the §11 `start()` order).
Gateway §24.8(2) nevertheless gives every Stage-2 guard a declaration
`accepts:{subject_ns:[…], admission:[…]}` — so an admission axis exists at Stage 2 with no Stage-1
producer. Meanwhile:

- PnP §8.3: "The export action is a pure authoring action, so its Stage-1 set is built entirely on
  **procedural facts**" — eleven named `pnp.*` rows.
- Turning §13.6: guards declare `accepts{subject_ns:[art:,job:,res:], admission:["procedural_fact"]}`;
  §15: "Turning contributes **no** Stage-1 evidence."
- Cut §9.2: "**No cut process has any probed device evidence and none advertises `execute`, so all three
  are Stage-1 ineligible by construction**" — while §9.3 ships a six-check Stage-2 release gate.
- Robot §6.4: "the only Stage-1-admissible robot evidence is **the existence of a seq-stamped Gateway
  journal row** for each required check."

DoD 14 permits gating on procedural facts; DoD 5 defines Stage 1 as probed-only. The seam between them
is never closed. As written, the cut, turning, PnP and robot release gates have no admissible Stage-1
source, so Stage 2 never runs and the gate is unfireable — the exact defect class metrology §"Guard
audit" and additive §8.2.1(ii) exist to catch, reproduced at batch scale.

**Required correction (Gateway, §10.1, W1.1).** Name exactly two admission classes and their scopes:
`probed_evidence` (`state=="ok" && src=="probed"`, the only class admissible for an execute-side subject
or an approval-bound device guard) and `procedural_fact` (a seq-stamped
`ProceduralFact{check_name, artifact_hash, config_epoch, snapshot_id?, at_seq, outcome}` row bound to an
`art:`/`job:`/`res:` subject, admissible only where §24.8(2)'s `accepts.admission` lists it). State that
`procedural_fact` admission can never satisfy an execute-side subject — the enforcement point already
exists in §24.8(2) and in §24.3(i)'s flip table. Extend §20 test 6 with: a `procedural_fact` fed to each
approval-bound device guard is refused by name.

### B2 — The corroboration rule contradicts itself, and test 6 asserts the opposite epoch relation (P0)

**Evidence.** Gateway §10.1 (lines 508-511): "`unsupported` may be re-read as `not_applicable` only when
independently corroborated for the **same subject and config epoch** by fresh evidence … **a corroborator
derived from the same source or epoch does not count**." Gateway §20 test 6 (line 923-924):
"corroboration rejects a same-source or **different-epoch** corroborator."

Any corroborator bound to the same config epoch is by definition derived from that epoch, so §10.1's
exclusion makes `unsupported → not_applicable` unreachable for every subject; and the prose and the
contract test demand opposite epoch relations, so `gates::corroborate()` cannot satisfy both. Five
consumers depend on the rule: gateway §23.1, monitoring §2.1.1(1), additive §8.2 ("must carry the same
`device_id` and `identity_fingerprint` **and be `state:"ok"` itself**"), metrology §"A corroborator must
be bound and must itself pass stage 1", robot §6.4.

**Required correction (Gateway, §10.1 + §20 test 6, W1.1).** The two axes are separate: *binding* =
same subject and same config epoch; *independence* = a different source/lane. Rewrite §10.1 as "the
corroborator MUST be bound to the same subject and config epoch, MUST be independently fresh, and MUST
NOT originate from the same source or lane as the claim." Test 6 then reads correctly as written.

### B3 — The E0-E3 normal gate cannot drive any approval-path gate to pass (P0)

**Evidence.** Four statements in one document cannot all hold:

1. §12 emulator boundary: "`kind()==emulator` forces `src:"simulated"` at the republisher, so an emulator
   can never satisfy Stage-1 admission for an execute-side subject."
2. §9 Tier-0 register: `mfg_run_start` requires `capability.execute == verified` **and**
   `emulator_stage.at_least_pilot`.
3. §18: "E0-E3 ceiling is `monitor` … only E6 unlocks `execute`", E6 = "pilot machine, attended".
4. §10.5: "Every gate in `required_set` has at least one source set that drives it to pass (**the
   emulator E3 golden run drives the full router set to pass**)"; §20 test 6: "every required gate is
   driven to pass by at least one source set"; §20 smoke `mfg-e0e3`: "propose → submit → approve →
   **start** → hold → resume-refused-after-alarm → abort."

Twin §9.5 states the consequence independently and correctly: "Since every twin output is
emulator-sourced by construction, no twin value can ever pass Stage-1 admission for an execute-side
subject." A `mfg_run_start` refused at Tier 0 cannot then be held or aborted, so the smoke's tail is
unreachable, and DoD 8's "approval-path owners drive every gate to pass from at least one source set"
has no hardware-free source at all. §22 row 8 nevertheless records DoD 8 as accepted at §10.5.

**Required correction (Gateway, §10.5 + §20 + §22 row 8, W3.2/W3.3).** Pick one and make all three
statements identical:
(a) Scope the end-to-end pass path to the `emulated` machine class of §14, whose required set is declared
separately and for which `src:"simulated"` is explicitly admissible **for that class only**, never bound
to an approval on a physical device — and have `mfg-e0e3` assert the *refusal* path for the router and
printer classes; or
(b) Delete the "drives the full router set to pass" claim, re-scope DoD-8 liveness to unit-level
pass/fail fixtures in `test_mfg_gates.cpp` (direct evidence injection, which §10.5 already provides per
guard) and record E4-E6 as the only source set that drives the router required set to pass end-to-end,
updating §22 row 8's "where" column accordingly.

### B4 — Printer `execute` advertisement is specified three incompatible ways (P1)

**Evidence.** Gateway §13: "the printer lane's `execute` level stays `assumed` until the
Moonraker/Klipper and OctoPrint-virtual-printer E-stages pass." Gateway §18: "only E6 unlocks
`execute`," E6 = pilot machine, attended. Additive §10 (lines 1098-1100): "Moonraker / OctoPrint
`transport`+`execute` are `probed` only after a successful handshake." DoD 18: "No capability level may
exceed the highest emulator/hardware stage actually passed." DoD product choice: "no physical printer
target until virtual qualification" — a necessary condition, not a sufficient one.

Gateway §13 reads as sufficiency; additive §10 reads as a handshake being sufficient; §18 and DoD 18
require E6 hardware.

**Required correction (Gateway §13 + additive §10, W5.2).** Virtual qualification raises **nothing** —
it is a precondition for attempting E4. Printer `execute` requires E6 on a pilot printer, exactly as
§18 says. Strike "probed after a successful handshake"; a handshake yields `transport` evidence subject
to the E4-E5 ceiling, and `execute` stays `assumed` with `missing_level:"execute"` until E6.

### B5 — Provenance laundering: the hardware-free printer fixture types as `probed` (P0)

**Evidence.** Gateway §3.2 derives `src` from transport kind: "`serial|tcp|websocket|http_adapter` →
`probed`; `emulator` → `simulated`". Gateway §13: "OctoPrint's **virtual printer** plugin is the
hardware-free E-stage fixture for the printer lane"; §20 smoke `mfg-printer-virtual` = "OctoPrint
virtual-printer + recorded Moonraker fixture lane"; additive §12 adds `fake_moonraker`. A virtual
printer is reached over HTTP, therefore `http_adapter`, therefore `probed` — so a **software** printer
produces Stage-1-admissible execute-side evidence, which is precisely what §12's emulator rule exists to
prevent. §24.5(2) widens the same door: "Because it is a real in-daemon transport, it **does** yield
`src:"probed"`" for `http_adapter`. Monitoring already recognises the hazard and keeps a second field
for it (§3.1: "a fixture replay and a loopback emulator are both `src="simulated"` but must be
distinguishable").

**Required correction (Gateway §3.2, W1.1).** Derive `src` from `(transport_kind, endpoint_is_physical)`.
Any endpoint recorded in the fixture manifest as virtual, emulated, recorded or loopback — OctoPrint
virtual printer, `fake_moonraker`, recorded Moonraker fixture lane, monitoring's replay, cut's
`grbl-sim` — forces `src:"simulated"` regardless of transport kind. Bind the flag to monitoring's
existing `origin` field so there is one mechanism, and assert it in `test_mfg_capability.cpp` and in
monitoring's `test_replay_determinism.py` (which already asserts every replay row is `simulated`).

### B6 — Additive claims `src:"probed"` for a computed preflight report (P1)

**Evidence.** Additive §8.2 (line 665-666): "the gating object is 'preflight report bound to
`artifact_id` A and revisions R, recorded at `seq` N' — appended durably by Gateway, **`src:"probed"`**,
bound to the artifact hash", and the five re-specified guards are "*Procedural guards (arbiter-computed,
gateway-recorded, bound to artifact hash + seq)*". Gateway §24 rule 3: "`src`/`admission` is computed by
the gateway, never accepted from a caller. `probed` is settable only by transport code inside the daemon
holding an open `ITransport`." Robot §6.2 withdrew this exact claim: "My earlier draft asserted
`src:"probed"` for 'procedural fact' rows. Gateway §24 rule 3 makes that structurally impossible."

An arbiter computation over artifact bytes and a declared profile is not transport-sourced. Left as
written, additive's five procedural guards are the only rows in the batch asserting `probed` without a
transport — an unsafe capability claim that would also defeat B5's fix.

**Required correction (additive §8.2, W-C).** Those rows are `admission:"procedural_fact"` (per B1),
not `src:"probed"`; their `src` is whatever the gateway derives (`declared` for the profile inputs), and
the profile's `declared` provenance stays the "separate visible fact in the binding" additive already
specifies.

### B7 — Additive and metrology re-declare `CapabilityTruth`, using `probed` as a truth value (P1)

**Evidence.** Gateway §23.1 fixes `CapabilityTruth.truth ∈ {verified|declared|assumed|unsupported|
not_applicable}` and §23 states "No domain redefines either; a second definition is a contract-test
failure." Monitoring §11.1 records the correction explicitly: "the truth vocabulary is
`verified|declared|assumed|unsupported|not_applicable` (**not** `probed` — `probed` is an observation
`src`, not a capability truth)". Twin §9.5, PnP §8.1, robot §6.1, turning §13.4 and cut §6.1 all compose
the gateway type verbatim. But:

- Additive §10 (lines 1081-1083): "Each level per device carries `{source: probed|declared|assumed,
  freshness}`", with `– not claimed` used as a third value.
- Metrology §1 (line 50): "Per device, per level, each carrying its own `{source, freshness}`."

Two of ten domains carry a divergent capability shape, and both use the axis (`source`) that B5 and B6
show is the one most easily inflated.

**Required correction (additive §10, metrology §1).** Adopt `CapabilityTruth` verbatim from
`core/device/capability.h`. `– not claimed` becomes `unsupported` with a `why` (the shape every other
domain uses); "probed after handshake" becomes `verified` with an `evidence_ref`, subject to B4's
ceiling; the `{source, freshness}` pair survives only inside metrology's uncertainty budget, where §3's
mapping note already says it is "a *different axis* — budget metadata, not a gate value."

### B8 — Additive mints its own durable `seq` (P1)

**Evidence.** Additive §3.6 (line 291-292): the run record's "line 1 is a `chudware.print_run/1` header
… every later line is one record with a **monotonically increasing `seq`**"; §8.4: "append-only,
`seq`-monotonic". Gateway §24 rule 1: "`mfg_event_append` is the only way any domain obtains a stamped
`seq`. … A domain that mints its own seq is a contract-test failure (§20 test 11 extends to every domain
bridge)." Every peer states the same: PnP §4.2 ("Minting, guessing, deriving, or carrying a `seq` that
did not come back from an append is a contract-test failure"), turning §13.3, cut §5.2 and §8 test 27,
robot §4, monitoring §2, twin §14.

Two `seq` namespaces over the same run is exactly the ordering ambiguity DoD 9's `seq <= at_seq` scoping
depends on being unique.

**Required correction (additive §3.6/§8.4).** Rename the file-local ordinal to `record_index` and take
`seq` from `mfg_event_append` for every row a UI may paint. Additive's own offer in §3.6 ("if Gateway
prefers to own the file, I emit the join record into theirs") is the cleaner resolution and should be
taken.

### B9 — Metrology computes its own invalidation closure and keeps a second durable store (P1)

**Evidence.** Metrology §6.3 "**Cascade rule**" (line 1140-1146): "Both channels are transitive, and
**the producer walks the graph** rather than leaving consumers to infer it. … **One event per painted
dependent**, each carrying `cascade_from` naming the root cause and its own `at_seq`." Gateway §24.1:
"`mfg_result_invalidate` computes the transitive closure over [`derived_from`] before applying the notice
— so a domain cannot forget a dependent row, and `affected[]` is the closure the gateway computed, **not
the caller's best guess**." Monitoring §8 ("This lane names only the **root subject**") and PnP §4.3
("**PnP does not compute `affected[]`**") comply; metrology is the one domain that does not.

Separately, metrology §6.3 "**Audit ledger**" (line 1148-1153) appends observations, proposals,
approvals, applies and inspection results to `cache/machines/<machine_id>/metrology_ledger.jsonl`. It
disclaims being a second ordering authority, but it is a second durable store of exactly the rows an
approval binds to, outside the single-writer journal of gateway §4.1.

**Required correction (metrology §6.3).** Supply `derived_from[]` edges on every `mfg_event_append`,
name only the root subject on `mfg_result_invalidate`, and let the gateway emit the closure and the
per-dependent notices. Keep the ledger only as a tamper-evidence mirror that quotes the gateway's `seq`
and `at_seq` verbatim and is never read as truth (the same status §24.2 grants a domain mirror event).

### B10 — Four domains' durable-result kinds are absent from the W1.1 table, so their durable surfaces and capability reports are refused (P0)

**Evidence.** Gateway §24.3(iii): kind-table presence is "a hard prerequisite, not a nicety: until a kind
is in the table, every append of it is correctly refused `mfg_unregistered_result_kind` and the domain's
durable surface does nothing." Gateway §24.11(4): "a domain that advertises any level above
`unsupported` without a registered `<domain>.capability` kind is refused
`mfg_capability_requires_durable_kind`."

Accepted for W1.1 across §24.2, §24.3(iii), §24.4, §24.6 and §24.11: PnP 11, cut 9 + `cam.cut.capability`,
twin 3 + `twin_capability`, robot 8 (incl. `robot_capability`), monitor 4 + `monitor_capability`. **Not
accepted anywhere in the gateway document:**

| domain | kinds it will append | paints a CapabilityReport? |
|---|---|---|
| turning | six `cam.turn.*` (turning §13.5), requested as an append in §18 | yes (§13.4, §17 panel) |
| metrology | observation, probe result, offset proposal, offset approval, inspection result, station profile, registration (+ capability) | yes (§1, §10 tier rows) |
| additive | slice result, print artifact, preflight, run link (+ capability) | yes (§10, §9 mirror panel) |
| perception | `aicad.perception_event/1`, `EvidenceClip` (+ capability) | yes (§1: advertises `monitor`) |

Each of those four advertises at least one level above `unsupported` (turning/metrology/additive five or
more; perception `monitor`), so §24.11(4) refuses their capability registration outright and §24.3(iii)
refuses every durable append. Their W1.x slices cannot be independently green.

**Required correction (Gateway §24.3(iii)/§24.11(1), W1.1).** Extend the accepted W1.1 set with
turning's six `cam.turn.*` plus `cam.turn.capability` (turning's own §13.5 already declares both columns
and the `*capability` non-inheritance), and require metrology, additive and perception to submit kind
tables carrying both §24.1 columns plus a `<domain>.capability` kind before W1.1 closes. W1.1 is not
green until the durable-result-kind table is total over every domain that appends.

### B11 — The `manufacture`-family-off-by-default mechanism has no owner and no wave (P0)

**Evidence.** Robot §3.7 is the only place in the corpus that establishes the mechanism, and it
establishes that omission does **not** achieve it: "`DEFAULT_TOOL_FAMILIES` (`:589-593`) does not contain
`manufacture`" and the `else` branch makes an unlisted family default-**on**, so "two edits are required
… add `"manufacture"` to `DEFAULT_TOOL_FAMILIES`, and add `DEFAULT_OFF_TOOL_FAMILIES = ("manufacture",)`
consulted in that `else` branch."

Gateway §17 asserts the property as already true — "The family is **OFF by default in
`agent.tool_families` until commissioning**, so the self-enable path (`enable_tools`) cannot silently add
motion surface" — and §22 row 15 records DoD 15 accepted. But gateway §2's "Existing files touched" list
does **not** contain `services/agent/config.py`, and §19's W4.3 delivers "agent surface, family off by
default" with no mechanism named. Seven domains register into that family (gateway §17, twin §9.1,
monitoring §4.4, cut §5.3, turning §12.2, PnP §9.2, robot §5.2).

Consequence: if any MCP wave lands before robot's config edit, the entire Manufacture surface ships
**enabled by default**, including gateway §17's three motion-class tools (`request_mfg_run_start`,
`request_mfg_hold`, `request_mfg_abort`). The one mitigation that survives is gateway §17's
`_motion_membership_allows` clause — which is itself in the same unowned file family (see B14).

**Required correction (Gateway, W1.1).** Move the two `services/agent/config.py` edits and
`test_manufacture_family_default_off.py` into W1.1, list the file in §2 as an integrator seam with
Gateway as sole owner, and make it a hard predecessor of W4.3 and of every peer's MCP-registration wave.
Robot keeps the analysis credit; it must not keep the ownership, because its wave is not first.

### B12 — Perception registers a second MCP family; additive and metrology name none (P1)

**Evidence.** Gateway §17 records Main's final ruling: "**one shared family id `"manufacture"` covers the
entire overhaul** — gateway, monitor, cut, turn, PnP, robot — so Settings → AI → Tools has a single
honest toggle for the whole Manufacture surface instead of six that drift apart." Perception §4.7 (line
492) and §10.2 (line 775) register `view(mcp, "perception")` as its own family; additive §5.3
(`services/mcp/print_tools.py`, tools `print_*`) and metrology §10 (`services/mcp/metrology_tools.py`,
tools `metrology_*`) name no family at all. Given B11's default-on semantics for an unlisted family, a
separate or unnamed family ships **enabled**.

Note the ruling's enumeration itself stops at six domains and never names metrology, additive,
perception or twin — twin §9.1 nonetheless opted in correctly.

**Required correction.** All three register through `view(mcp, "manufacture")`, keeping granularity via
the existing `get_tool_options` per-tool toggles exactly as every other domain does; and gateway §17's
enumeration is extended to name all ten domains so the next contributor does not read the list as
exhaustive.

### B13 — `contracts/worker_rpc.md` §7 is claimed twice, and the `contracts/events.md` §5 amendment is unowned (P1)

**Evidence.** Gateway §2: `contracts/worker_rpc.md` gains a "new append-only `## 7. Manufacturing
gateway (mfg_*)` section". Monitoring §6.1: "`contracts/worker_rpc.md` — proposed new **§7** 'Manufacturing
monitor (read-only)'". Additive §2/§5.2, PnP §2(1), robot §10(1) and turning also append to the same
file. Two documents claim the same section number.

Worse, monitoring §6.3 names a required rule amendment it explicitly does not apply: "`contracts/events.md`
§5 currently forbids emitting events after the final response line. A monitoring stream contradicts that
on the request lane. **Dependency on `GatewayDesignRecovery`** … Absent the amendment, wave 2 ships
**poll-only**." Gateway §15's `mfg_device_subscribe` ("streams `mfg.device.*`") and `mfg_event_subscribe`
("streams journal records + invalidation notices") need exactly that amendment, and gateway §2 lists
only "append `mfg.*` event kinds" for that file. Monitoring §6.4's `observer` client-kind append to
`contracts/worker_rpc.md` §5.1 is likewise unowned, yet gateway §7 depends on the observer/commander
split being expressible ("an observer socket cannot reach any write dispatch path").

**Required correction (Gateway, W1.1 for numbering, W4.1 for the rules).** Gateway allocates one
`contracts/worker_rpc.md` section per domain in W1.1 and publishes the allocation; Gateway owns the
`contracts/events.md` §5 subscription-lane rule and the `observer` client kind in W4.1, since its own
streaming RPCs and its own lease model depend on both. Without this, monitoring's W2 degrades to
poll-only *and* the gateway's two subscribe methods are non-conformant with the contract they cite.

### B14 — `services/mcp/tool_registry.py` has five claimants in overlapping waves (P1)

**Evidence.** Gateway §17/§2 (add `RISK_MOTION`/`RISK_SAFETY` to `RISKS`, `_MOTION_TOOLS`/`_SAFETY_TOOLS`,
one `deny_reason` clause; ~25 lines, W4.3). Robot §5.2: "Exact `tool_registry.py` append (after `:69`)".
PnP §9.2: "Append to `_RISK_OVERRIDES` (integrator-applied): `"run_pnp_harness": RISK_EXTERNAL`,
`"pnp_simulate_job": RISK_WRITE`, `"pnp_define_panel": RISK_WRITE`". Additive §2 lists the file as
modified. Perception §10.2: "add `perception` to the default family/risk table | ≤ 3 lines". The batch
constraint is one owner per shared file per wave.

The collision is not merely textual: gateway §20 test 12 asserts "`classify_risk` output for every
pre-existing tool is unchanged", which four concurrent appenders can each break for the others.

**Required correction.** Gateway owns `services/mcp/tool_registry.py` for the risk-class change in a
single wave (W4.3, or W1.1 if B11 moves the family default there); every other domain's rows land as data
appended by that owner or in a strictly later wave; `test_mfg_tools.py`'s unchanged-`classify_risk`
assertion is the shared acceptance check for all five.

### B15 — `core/cam/post.cpp:487` requires a joint edit by two owners (P1)

**Evidence.** Turning §18: "`CuttingProcessArchitecture` … touches only the head branch of `post.cpp`
(`:401-402`, `:454-470`, `:482`, `:487`) — note that **`:487` is the shared footer line: my change
deletes the `!g.lathe` condition while theirs adjusts the laser condition, so that one line needs a joint
edit** and is called out here as the single true overlap in the batch." Cut §3.2 asserts the opposite
posture: "The seams below are **disjoint** from its lathe/CSS/diameter seams by line region and by
branch."

**Required correction.** Assign `post.cpp`'s footer line to exactly one owner for the wave — turning,
since it *deletes* a condition that cut only *adjusts* — and have cut consume the result. Record the line
in the wave ownership table rather than in one document's coordination note, and reconcile cut §3.2's
"disjoint" claim, which is false as written.

### B16 — The UX slice is frozen by summary; its schemas and gate rows are not in the corpus (P1)

**Evidence.** `agent://ManufactureUxArchitecture` contains only a freeze summary. It asserts a frozen
deliverable ("6 surfaces … 15 new files under `host/src/ui/manufacture/**`; 12 existing files named to
the line; a 2-member `app_state.h` append", "Tier 0 construction-refusal register (8 refusals)", "the
compound-check downgradable/non-downgradable register", "the pass-direction table columns") but none of
that content is in the artifact. Peers bind to it as a hard dependency:

- Additive §8.2.1(ii): unsatisfiability of a gate's whole source set "belongs to the approval-path owner:
  Gateway for the arbiter-side preflight set, **UX for the run-approval rows**."
- Metrology §"Guard audit"(ii): identical assignment, and it names this as how the last unsatisfiable
  gate escaped: "an unsatisfiable gate survived anyway."
- Metrology §10 claims no GUI files and depends on UX-owned `host/src/ui/manufacture/inspectors.cpp`.
- Additive §2 records "Shared-file collisions … with UX (UX lands `app_state.h` + `mfg.mode` first)".
- The artifact's own closing line lists still-open items: "Gateway's five obligations … and your decision
  on `exercisable_by` selectability" — the latter was subsequently decided (DoD product choice 1,
  gateway §8.1), so the artifact is also stale.

DoD 8 requires that "approval-path owners drive every gate to pass from at least one source set". One of
the two approval-path owners has published no gate list, so that obligation is undischargeable for the
run-approval rows — and B3 shows the other owner's discharge is also broken.

**Required correction.** Recover UX's run-approval row set, the 8-row Tier-0 register, the
downgradable/non-downgradable register and the 12 named existing files into a `local://` artifact before
W1.1 closes, or reassign the run-approval bullet-(ii) audit to Gateway in writing. Until one of those
happens, W6 and every peer panel that "calls UX's shell" has an unreadable interface.

---

## CORRECTION REQUIRED (non-blocking for W1.1, blocking for the owning slice)

### C1 — Additive path/prefix divergence, and an incomplete reservation table (P2)

Main's ruling (gateway §0.9): "production GUI additions live **only** under
`host/src/ui/manufacture/**` (no `ui/mfg`, no new canvases); cross-domain service root is
`services/manufacturing/**`; … RPC/events stay `mfg_*`/`mfg.*`". Additive diverges three ways:
`GUI tree host/src/ui/print/` (§0 line 7, §2 line 95, §9 line 1055, §11 line 1122); RPC prefix `print_*`
(§5.1, §5.3) rather than `mfg_*`; and `services/gateway/**` as the gateway tree (§2 line 139, §12 line
1218) where gateway §2 actually creates `services/manufacturing/gateway/**`.

Gateway §0.10's reservation table also names only monitor, cut, turn and robot. Unreserved and in use:
`print_*` / `mfg.print.*` (additive), `mfg_twin_*` / `mfg.twin.*` (twin §9.1-9.2), `mfg_metrology_*` /
`mfg.metrology_*` (metrology §6), `mfg_perception_*` / `mfg.perception_*` (perception §10.3),
`mfg_tier0_*` / `mfg_result_*` (gateway §24, announced but not added to §0.10).

**Correction.** Extend §0.10 to all ten domains plus the two new gateway prefixes; move additive's GUI
under `host/src/ui/manufacture/print/**` or amend the ruling once, explicitly naming additive as the sole
exception and why; `services/gateway/**` → `services/manufacturing/gateway/**`; and either rename
`print_*` → `mfg_print_*` or record the prefix exception where the ruling lives.

### C2 — The DoD-16 ABI gate is lexical, and additive's C ABI surface bypasses it by construction (P2)

DoD 16 and gateway §20's `mfg-abi` smoke assert only that "`libchudware` exports contain no `mfg_`
symbol". Additive §5.1 appends **sixteen `print_*` methods** to `core/api/libchudware.cpp` after the
`cam_*` block. Nothing about the gate notices, because the gate matches a prefix. Additive's §0 states
the substantive property that actually matters — "**No `print_*` method may open a transport, take a
lease, upload, or start anything**" — and that property is untested.

**Correction.** Keep additive's authoring-only ABI placement if that is the decision, but say so in
`contracts/manufacturing_gateway.md`, and strengthen `mfg-abi` to assert the C ABI carries no transport,
lease, run, approval or device-command entry point **regardless of prefix**. Mechanize additive §0's own
sentence rather than trusting a name.

### C3 — Invalidation event dispatch is specified five different ways (P2)

DoD 9 and gateway §5 define one envelope. Dispatch, as specified: gateway §16 emits **two** names
(`mfg.result.falsified` / `.superseded`); twin §9.2 explicitly rejects name-splitting ("the earlier
proposal … to split falsified/superseded into two event names is **superseded** by DoD v4 §9 and is not
implemented") and emits one `mfg.twin.invalidated` + `class`, as do cut §5.2, PnP §9.3, robot §6.5 and
monitoring §8; metrology §6.1 states the rule correctly ("consumers branch on `class`, never on an
event-name table, so an event added later that forgets to set `class` fails immediately instead of
silently falling outside a name-matching table") and then lists per-trigger falsified and superseded
event names; additive §8.2.1 and perception §4.1 (line 263) make the name split the design ("splits three
cases across three distinct event names").

The consuming dispatch point is the host lane drain, which metrology §6.3 and perception §4.1 both cite
to the line (`host/src/core/engine_events.h:3`, `engine_events.cpp:907-909`, `host/src/app.cpp:3859`). A
name-keyed consumer there silently drops any name added later — exactly metrology's own stated failure
mode.

**Correction.** One invalidation event per domain (`mfg.<domain>.invalidated`) carrying the §5 envelope;
`class` carries alarm-vs-quiet, `reason` carries the per-trigger distinction. Gateway's two-name pair is
the single ratified exception and is documented as such. Additive's and perception's cry-wolf argument is
already satisfied by `class`.

### C4 — Monitoring §11.2 still says E1/E2 are gate-skippable (P2)

Monitoring §11.2 (line 768-769): "E1/E2 are skippable with a documented absent prerequisite, and a skip
pins the level at `assumed` — a SKIP can never read as a pass." Monitoring §4.6 (line 400-410) and
gateway §24.4/§24.10 say the opposite and are correct: the stage servers "are never invoked by
`scripts/gate.sh`; they run only from opt-in qualification scripts", the batch adds **zero** SKIPs, and
"pinning capability to `assumed` with the missing level named beats a SKIP". The DoD-1 five contain no
MTConnect/OPC-UA server.

**Correction.** §11.2 reads "excluded from `gate.sh` behind `scripts/qualify_monitor_e{1,2}.sh`; absence
pins the level at `assumed` with the missing level named — not a SKIP", matching §4.6.

### C5 — Twin §11 and §14 contradict each other on the twin-binary SKIP (P2)

Twin §11 (line 1104-1110): "the twin adds **no new normal-gate SKIP** … those checks are **excluded**
from `scripts/gate.sh` rather than reported as a SKIP, so coverage never depends on it." Twin §14 (line
1244-1246): "`CHUDWARE_TWIN_BIN` follows the existing `CHUDWARE_*_BIN` pattern and **asks for exactly one
documented SKIP**." DoD 1 does list "an optional twin binary" among the permitted five, but gateway §20
and §24.4 assert the batch adds zero and that "the gateway suites themselves never SKIP".

**Correction.** §14 adopts §11's wording (exclusion with an asserting test, the pattern §24.4 names as
correct). If the DoD-1 twin-binary SKIP is retained, name it as pre-existing and point it at the specific
gate step that owns it, not at twin's suites.

### C6 — Twin's prohibited-word grep fails against twin's own corpus (P2)

Twin §7 (line 744-746): prohibited "anywhere in twin code, contracts, docs or UI strings, enforced by
`test_twin_fidelity.cpp` as a source grep: `certified`, **`safe`**, `verified safe`, `crash-proof`,
`collision-free`, `virtual commissioning`, `step-accurate`, `Grbl-exact`, `guaranteed`." Twin's own §1.2
table header (line 83) is "**Why it is safe**", §11 says "Every test is deterministic, isolated and
**full-suite safe**", and the token is a substring of `safety`, `unsafe`, `safety_integration` and
`safety_confirmed`, which twin uses throughout §14 and §9.

As specified the gate can never pass — the unfireable-gate shape this corpus exists to eliminate,
reproduced in a truthfulness test.

**Correction.** Match whole claim phrases with word boundaries, keep the multiword claims verbatim, scope
the grep to UI strings, capability text and contract prose rather than all docs, and enumerate the
allowed exceptions (`safety*`, `unsafe`, "full-suite safe") in the test.

### C7 — `emulated` machine class claims `safety_integration: not_applicable` uncorroborated (P2)

Gateway §14's `emulated` row sets `safety_integration` to `not_applicable` while the same row says `src`
is "forever `simulated`". §10.1 and §23.1 permit `unsupported → not_applicable` only with an
independently fresh, subject/config-bound corroborator, and DoD 7 forbids `not_applicable` for a whole
action prerequisite. A class whose only evidence is simulated can have no admissible corroborator, so the
value is unreachable.

**Correction.** `unsupported` with a `why` ("emulated class has no interlock circuit"), matching every
no-device-path domain in the batch (twin §9.5, PnP §8.1, robot §6.1, turning §13.4, cut §6.1).

### C8 — Capability rows: FreshValue-wrapped by three domains, never-wrapped by PnP (P2)

Gateway §15: "every numeric or enum field a UI paints is a `FreshValue<T>`". Metrology §3: "Every field
the GUI paints is wrapped in UX's `FreshValue<T>`, verbatim". Additive §3: "Every field the GUI paints or
an event carries is wrapped in UX's `FreshValue<T>`". Robot §6.2 lists `robot.capability` as a
`FreshValue` row a UI paints. PnP §4.1 (line 218-222) forbids it: durable authored artifacts "carry `seq`
+ `revision` + `artifact_hash`, **not** a freshness envelope … This distinction is stated in
`contracts/mfg_pnp.md` so no later contributor 'helpfully' wraps a job artifact in a `FreshValue`" —
while §24.11 now makes `pnp.capability` a durable row PnP's GUI paints (§9.4).

**Correction.** One ruling: a durable authored row is journaled **and** published wrapped, with
`ttl_ms:null` and `state:"ok"` for rows that do not decay. PnP §4.1's prose narrows from "no envelope" to
"no wall-clock TTL", which is the distinction it actually argues.

### C9 — Turning's fixtures diverge from the ratified `fixtures/mfg/**` root (P3)

Monitoring §"Path/naming ruling applied" records "fixtures under `fixtures/mfg/**`" as part of the
ruling, and gateway §2 (`fixtures/mfg/gateway/**`), twin §12 (`fixtures/mfg/twin/e<N>/`), cut §3.5
(`fixtures/mfg/cut/**`), PnP §10 (`fixtures/mfg/pnp/`), monitoring §4.5 and robot §3.6
(`fixtures/mfg/robot/`) all comply. Turning §16.3 (line 1021-1024) places its fixtures under
`services/manufacturing/turning/fixtures/` "because no `fixtures/` directory exists in this repo" — which
six sibling slices create in W1.x regardless.

**Correction.** `fixtures/mfg/turn/**`.

### C10 — Cross-reference drift on the two most load-bearing §24 rules (P3)

The procedural-fact flip scope is gateway **§24.3(i)**. It is cited as §24.4 by cut (§6.1 line 625, test
32) and as §24.6 by turning (§14 line 861) and robot (§6.1 line 677, §6.4). The read-only transport split
is **§24.5**; monitoring cites §24.1 in its §2.1 heading. Subject namespacing is **§24.8**; turning cites
§24.10 (§13.6 line 778). §24.4 is the robot kind table, §24.6 is the monitor kind table, §24.10 is the
`monitor_condition` relabel — so three of these citations point at unrelated rules.

Because gateway §20's contract tests are specified *by section*, these citations become wrong assertions
the moment someone implements them literally.

**Correction.** Normalize every cross-reference against the final gateway numbering in one W1.1 pass, and
have `test_mfg_contracts.cpp` key on stable rule ids rather than section numbers.

---

## PASS — properties verified consistent across the corpus

These were audited specifically and found clean; each is recorded so a later change cannot quietly
regress it.

- **P1 — `FreshValue<T>` has exactly one definition.** DoD 4's field list appears verbatim in gateway
  §0.2/§3.2, twin §14, monitoring §3.5, cut §4.4, turning §13.3, PnP §4.1, robot §6.2, additive §3,
  metrology §3, perception §4.6. The superseded `TelemetryValue{v,unit?,mono_ns,age_ms,trust}` appears
  nowhere; gateway §20's grep-assert against a second envelope with `trust`/`mono_ns` fields is the right
  mechanism. `v` null unless `state=="ok"`, display-only `last_ok`, and gap⇒unknown are stated
  identically everywhere. Only the *scope* of wrapping diverges (C8).
- **P2 — `CapabilityTruth` composition.** Eight of ten domains compose gateway §23.1 verbatim and
  explicitly refuse to re-declare the nine levels, the truth vocabulary or the stage ceiling (twin §9.5
  and §14, PnP §8.1, robot §6.1, turning §13.4, cut §6.1, monitoring §11.1). Two diverge (B7).
- **P3 — No-device-path domains report the four device-side levels `unsupported` with a `why`, never
  `not_applicable`.** Uniform across twin §9.5, PnP §8.1, robot §6.1, turning §13.4, cut §6.1,
  monitoring §11.1 — matching gateway §23.1's rule. The single exception is gateway's own `emulated`
  class row (C7).
- **P4 — Tier-0 `class` is always `PREREQ`; missing evidence fails; refusal renders at the action entry
  point; a later guard is defense-in-depth.** Stated in the same words by gateway §9/§23.2, twin §9.4,
  monitoring §11.3, cut §9.1, turning §11, PnP §8.2, robot §6.3, additive §8.2, metrology §"Tier 0". The
  one-register/two-entry-points ruling (§24.3(ii)) is honoured in both directions: cut §9.1 and PnP §8.2
  use the RPC, twin §9.4 and turning §11 call `registerRows` in-daemon and ship no Python table. Additive
  §8.2's "Tier 0 must be INEXPRESSIBLE, not INVISIBLE" and UX's identical completing rule are consistent
  with §9's rendering contract.
- **P5 — `procedural_fact` flip scope.** Every domain that flips a level does so only within
  `plan|generate|post|parse|simulate` and asserts the negative for the four device-side levels: twin §9.3
  ("flips exactly one level — its own `simulate`"), cut test 32, turning §14, PnP §8.3, robot §6.2/§6.4.
  §24.7's mandatory `supersede_with_parent:true` on every `*procedural_fact` kind and §24.9(3)'s
  prohibition on `*capability` kinds are honoured by PnP §4.2, cut §5.2, turning §13.5, robot §6.5 and
  monitoring §2.2. Only the section *citations* drift (C10).
- **P6 — Subject namespacing (§24.8).** Declared sets are disjoint and correct: monitoring `{src:}` only
  (§2.2), turning `{art:, job:, res:}` (§13.6), PnP `{art:, job:, res:}` (§4.2), robot `{art:, res:}`
  (§6.5), device lane `dev:` only. Each domain independently states that a fresh probed `src:`
  observation fed to a non-declaring guard is refused **by name**, and the cross-lane contract test in
  §24.8(3) covers the case whether or not a colliding subject is ever minted. This is the strongest part
  of the corpus.
- **P7 — Monitoring's read-only posture.** The five-layer amputation (§5), the frozen `FROZEN_METHODS`
  equality assertion rather than a subset check, `IReadTransport` with no `write()` (gateway §24.5), the
  lease being defined over `ITransport` only, and the §24.10 `monitor_condition` relabel are all
  internally consistent and consistent with gateway §7. Monitoring §2.2 already carries the corrected
  `falsify + retain_as_last_ok` label and the load-bearing asymmetry that `monitor_source_state` must
  **not** retain — with the reason written down, which is what will stop the "make it consistent"
  refactor.
- **P8 — Copyleft boundaries.** No in-process GPL/AGPL coupling is proposed anywhere: PrusaSlicer stays
  an out-of-process AGPL sidecar with corresponding-source artifacts (additive §4.1/§4.5, DoD 12/17);
  Klipper/Moonraker (GPLv3), OctoPrint (AGPLv3) and PrusaLink (freeware) are wire-protocol clients only
  (gateway §13, additive §6.5); LinuxCNC is invoked never linked (turning §14); OpenPnP is a user-supplied
  out-of-process checker (PnP §1); QtPlasmaC/grbl-sim are data-modelled never copied (cut §10);
  `cppagent` is an optional fixture generator (monitoring §9.5); ROS/MoveIt source is in-tree but built
  by no Chudware target and in no release artifact (robot §2.2/§9.2); Ultralytics is excluded and every
  reused weight is BSD/Apache/MIT (perception §3.4/§9). The `mfg-license` fast gate (gateway §20) and
  additive's fatal `scripts/check.py` rules cover the enforcement. **One structural gap only:** the ABI
  gate's lexical matching (C2). No GPL/AGPL boundary leak found.
- **P9 — Absence-not-denial for device verbs.** Jog, home, zero, unlock, override, interlock bypass,
  settings/firmware write, E-stop, lease preempt, waiver grant and self-approval have no RPC and no MCP
  tool in any of the ten domains (gateway §15/§17/§21, twin §9.1, monitoring §5, cut §5.1, turning §12,
  PnP §9.2, robot §5.1, additive §5.3, metrology §10). `_SAFETY_TOOLS` is empty by design and every
  domain re-asserts it. Approved-job autonomy is stated identically by gateway §11, additive §5.3 and
  metrology §10 (agent may request start against an active `human_or_agent` approval, may request
  hold/abort, may never authorize) — additive §5.3's explicit "this is a namespace boundary, **not** an
  advisor-only posture" is the correct reading of DoD 12 and does not conflict with anyone.
- **P10 — Seq-scoped, push-based invalidation.** `seq <= at_seq` scoping, producers push, no synchronous
  hold, replay idempotence, and the frame-ordering justification (`st.rpc.drain()` before the event tick)
  are stated compatibly by gateway §5, additive §8.2.1, metrology §6.3, perception §4.1, PnP §4.3, robot
  §6.5, monitoring §8, cut §5.2, twin §9.2. Only the *event naming* diverges (C3) and only metrology's
  closure ownership diverges (B9).
- **P11 — No-transparent-resume.** DoD 13 is honoured with terminal states and no return edge (gateway
  §6.2), mirrored inside the simulator (twin §3.2, §12 E3), asserted for printers (additive §8.3, and
  OctoPrint's `restart` deliberately unexposed, gateway §13), and no resume tool exists for any caller.
  Gateway §6.2's five-condition `held → resuming` rule is the only resume path and is strictly narrower
  than DoD 13 requires.
- **P12 — SKIP tally.** Zero new normal-gate SKIPs across the batch, with the "exclusion plus an
  asserting test" pattern used by cut (test 23), turning (§14), PnP (§7, `test_gates.py`), robot (§3.7),
  monitoring (§4.6) and twin (§11). Two stale sentences remain (C4, C5); the tally itself is sound and
  the documented list stays the DoD-1 five.

---

## Go/no-go

**NO-GO for W1.1 as the corpus stands.**

Three findings make W1.1 itself non-green and must land in W1.1 or before it:

1. **B10** — the durable-result-kind table is not total over the domains that append to it, so turning,
   metrology, additive and perception are refused `mfg_unregistered_result_kind` and
   `mfg_capability_requires_durable_kind` on day one. W1.1's whole purpose is to unblock the peers; it
   cannot do that while four of them are absent from the table it publishes.
2. **B11** — the `manufacture`-family default-off mechanism has no owner and no wave, and omission
   yields default-**on**. Every MCP registration wave is unsafe to land until it exists.
3. **B13** — one `contracts/worker_rpc.md` section number is claimed twice and the `contracts/events.md`
   §5 subscription rule the gateway's own streaming RPCs depend on is unowned.

Five further findings block the arbiter/gates slices (W3.2, W3.3) and must be resolved before a single
guard is implemented, because each of them determines what the guard code *is*: **B1** (Stage-1 admission
classes), **B2** (corroboration), **B3** (what the E0-E3 gate proves), **B5** (provenance of virtual
endpoints), **B16** (the missing half of the DoD-8 liveness obligation).

Four block their owning slices only: **B4**/**B6**/**B7**/**B8** (additive, W5.2/W-C), **B7**/**B9**
(metrology), **B12** (perception, additive, metrology MCP waves), **B14**/**B15** (shared-file ownership,
W4.3 and the CAM post wave).

**Smallest edit that turns this to GO:** a single W1.1 amendment pass on
`local://manufacturing-gateway-final.md` closing B1, B2, B3, B5, B10, B11, B13, C1, C7 and C10 — all ten
are gateway-owned text — plus one paragraph each from AdditiveArchitecture (B4, B6, B7, B8, C1, C2),
MetrologyArchitecture (B7, B9), PerceptionArchitecture (B12), MonitoringArchitecture (C4),
TwinDesignRecovery (C5, C6), TurningArchitecture (C9, B15 ownership) and CuttingProcessArchitecture
(B15 acknowledgement), and recovery of the UX row set (B16). No product scope changes, no new rules, no
deferral of any workstream: gateway, CNC/probing, twin, additive, MLDP perception, UX, industrial
monitoring, turning, laser/plasma/waterjet, PnP virtual flow and robot offline planning all remain in
scope exactly as accepted, and physical execution stays deferred to the approved Shapeoko/Grbl and
printer adapters behind E6.
