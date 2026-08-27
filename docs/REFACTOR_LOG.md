# Refactor Log

Running handoff log for the CAD modular refactor + GUI abstraction. Each node
appends a one-paragraph handoff (files touched, deferred, gotchas). See
`AGENTS.md` and the plan file for context.

## Archive

Entries through 2026-07 are in [`refactor-log/2026-07-and-earlier.md`](refactor-log/2026-07-and-earlier.md) (73 dated July entries plus the
undated P0/refactor-node sections). This file carries 2026-08 onward.

## 2026-08-03 — Holster-run inspection and verification feedback repair

Desktop runs `run-88954488724871` and `run-19345781609857` showed two separate failure
classes. The model built a rectangular primitive stack instead of the requested contoured
holster, then incorrectly judged its own annotated contact sheet as correct; the modeling
operations themselves all succeeded. The later audit exposed real harness defects:
`contact_sheet`'s first durable path could be deleted by its own cache prune,
`inspect_section(plane="XZ", offset=0)` rejected a complete section request, and the
live-turn battery mirror converted `ok:false` findings plus zero scores into PASS.
`services/mcp/cad_common.py`, `cad_eval_tools.py`, `services/verify/battery.py`, and
`services/agent/agent_turn.py` now preserve the first render directory, support named/custom
offset section planes, rewrite `watertight` exactly, teach that the kernel does not yet
prove single-body self-intersection instead of silently substituting a weaker check, run
`dfm_check` as a real battery check, and share the acceptance grader's honest PASS/WARN/FAIL
semantics. Focused regressions plus a live box-with-through-hole probe verify
HLR ASCII, a four-view contact sheet, shifted section areas, and the combined battery output.

## 2026-08-03 — General-lane promise stalls and sketch/edit/export dialect gaps

Live desktop run `run-22629074806645` (k3-256k) stopped after five calls having written
only a reference document, and reported success; the closing message was "Now updating the
task list and starting the model." Nine live replays (`probe-holster-1` … `-10`) turned that
one complaint into six general defects. In `services/agent/agent_turn.py` the promise
detector missed a subject-dropped coordinated tail ("… and will now load the modeling
packs"), a temporal lead-in ("Now let me confirm …") and a plan-introducing colon
("Final checks: …"), and — worse — a REPEATED promise fell through the identical-reply
guard and was banked as a `confirmed` finish; it now escalates once with the tool
vocabulary and then ends the turn honestly, and the distinct-narration allowance rose from
3 to 8 because a run died with 23 of 30 tool calls unspent. `services/agent/guardrails.py`
no longer hard-blocks sketch-scoped ops whose `refs` are entity handles (`e2`, `e1.p1`) —
they were checked against the FEATURE tree and always read as dangling.
`core/doc/session.cpp` reserves caller-authored entity ids before expanding a rectangle or
polygon, fixing "duplicate 'e2'" for any batch mixing an expandable shape with an
explicitly-id'd sibling. `services/mcp/cad_doc_tools.py` accepts the endpoint+centre,
three-point (`arc3`) and `start_angle`/`end_angle` arc dialects, runs the standalone
`sketch_add_constraint` op through the same constraint normalizer the inline path has
always used (including "type from `kind`"), aliases the delete/remove op spellings, and
hoists an `edit_feature` target out of a `feature` wrapper; `cad_exchange_tools.py` reads
`export_part(format=…)`. Measured end to end: the same request now runs 25 tool calls,
builds a 155 x 34 x 100 mm holster shell with cavity, snag hook and fillets, passes the
battery, and exports STEP + STL.
## 2026-08-03 — T-DP16: sub-agent control plane + session fork

Files: `services/agent/{subagent_registry,subagent_rpc,agent_turn,delegation}.py`,
`services/agent/test_subagent_registry.py`, `host/src/ui/ai/ai_workspace.cpp`,
`host/src/ui/copilot/{conversations.cpp,copilot_internal.h,panel.cpp}`,
`contracts/worker_rpc.md` §6, `work/tasks/T-DP16.md`.

The card was stale in the same way T-BL51 was: it framed `ui/imgui` as the only live GUI
(retired — `host/` is the single GUI target) and asked for a workspace that already shipped
at ~5.3k lines under `host/src/ui/ai/`. Rewrote the card against the observed surface first,
in its own commit, then built only the two genuine gaps.

Design point worth keeping: every new sub-agent control is enforced at the ONE pre-existing
turn boundary in `agent_turn.run_turn` (`if subagent_id:` at the top of the completion loop),
next to the steer drain and terminate check — the file-backed registry is the cross-process
control plane and `run_turn` is the single place it is obeyed. Nothing is a hard kill; both
holds poll at 0.5 s and are capped by `CHUDWARE_SUBAGENT_HOLD_MAX_S` (pause auto-resumes on
expiry, an unapproved spawn denies fail-safe). Do not add a second enforcement site.

Latent bug fixed in passing: `routing.yaml` sets `approval_policy.deeper: overseer`, and
nothing in the codebase ever called `DelegationTree.approve()` — so every depth>1 delegation
was registered `pending` and stranded forever while `delegate()`'s docstring told the caller
to "proceed with sibling work". `request_delegation` now marks those sessions
`awaiting_approval`, and the GUI approval queue releases them.

Deferred: the delegation TREE object itself is still never reconciled with the registry's
decision (the tree lives in-process in a mission run; the registry is the durable mirror the
GUI speaks to). If a future task needs `DelegationNode.status` to agree with the registry,
that reconciliation is the missing piece.

Gotcha: `forkConversation` must read `st.chat` — not `conversations[idx].msgs` — when forking
the ACTIVE session, and must zero `step.thumb.id` on the copy or the fork and its source share
a GL texture handle.

## 2026-08-03 — T-X7b: vendor registry key fix + scraped price into bom_price

Files: `core/api/libchudware.cpp`, `core/sourcing/{vendorreg,sourcing,specmatch,providers}.cpp`,
`core/sourcing/sourcing.h`, `core/tests/test_sourcing.cpp`, `services/mcp/parts_tools.py`,
`services/browser/queue.py`, `services/mcp/test_parts_tools_v2.py`,
`contracts/sourcing_schema.md`, `work/tasks/T-X7b.md`.

The headline is a latent bug, not the feature. `vendorreg.cpp` wrote index entries keyed
`slug(supplier)::slug(part_number)`; the production `Seams::vendor_lookup` glue in
`libchudware.cpp` looked up a bare `slug(mpn)`. They never agreed, so a registered vendor part
was invisible to `bom_rollup`/`bom_price` — DoD item 2 could not have worked no matter what fed
it. Both paths now call one shared `sourcing::vendor_index_lookup(index, component)`; keep it
that way, and keep the two-supplier regression test, or they will drift again.

`register_local_cad` is now a merge-preserving upsert over a `fields` json object rather than
seven positional args. `cad_path` is optional — a price-only record is legitimate and must never
blank an existing CAD file. A malformed `price_breaks` entry throws instead of being silently
skipped by `unit_price_for_qty` (bom.cpp:442), which would otherwise read as a missing price.

Anti-fabrication rule worth preserving: a scraped display price is parsed ONLY when unambiguous.
`"$12.34"` → 12.34; `"$10.00 / pack of 50"` and `"$5.00 - $9.00"` leave `unit_price` null and
return the raw string as `price_unparsed`. A wrong price silently entering a BOM is far worse
than a missing one. Every registered price carries `provenance.scraped: true`,
`provenance.method: "cef_browser_scrape"` and a `fetched_ts`.

Gotcha for future probes: `core/build-linux` was 3 days stale while `core/build-occt-linux`
(which takes precedence in `core_bridge`'s search order) was fresh — a live measurement can pass
against one tree and fail in CI against the other. Rebuild both.

## 2026-08-03 — delegation/registry reconciliation + three red gates

Files: `services/agent/{delegation,mission_dispatch,subagent_registry,subagent_rpc,agent_turn}.py`,
`services/agent/roles/general_agent.yaml`, `services/agent/advisor/triggers.py`,
`services/agent/tool_guidance/parts.py`, `host/src/ui/tool_panel_tables.{h,snapshot.json}`,
tests in `services/agent/`.

Closes the item deferred from T-DP16. `reconcile_registry_decision` /
`await_registry_decision` in `delegation.py` are the ONLY sanctioned bridge between the durable
registry and the in-process tree — do not reach into `tree._nodes` from the dispatch layer.
`mission_dispatch` now parks on the decision instead of returning a stranded `pending` node.

**The trap to remember: an approval hold must never fire unattended.** A headless bench/CI run
has nobody to approve, so a naive hold turns every depth>1 spawn into a 15-minute stall. The
registry therefore carries a watcher heartbeat — `subagent_list` (the GUI's ~4 s poll) stamps it,
and both holds check `watcher_active()` first. Measured: unattended returns in 0.2 s and leaves
the node pending exactly as before; attended parks and releases on the human decision. If you add
a third hold anywhere, gate it the same way.

Three gates were red on master before this work (proven pre-existing by stashing). The
substantive one: `general_agent` ended its card with `family: plm`, and family expansion fills
leftover seats **alphabetically** — with three seats left it granted plm_init/plm_open/plm_save
and dropped plm_item_add and plm_checkpoint, while the prompt told the model to checkpoint and
the handoff schema demanded `checkpoint_commit` as evidence. Never end a card with a `family:`
entry whose tail you depend on; name the tools. The seats came from
`run_linear_role_turn`, which advertised only `SUBAGENT_DEFAULT_TOOLS` — so the main lane could
call `run_shell` but was never told about it (permitted-but-unoffered, the mirror of
advertised-but-dead). It now unions `AGENT_DEFAULT_TOOLS`; deliberately NOT
`SHARED_DEFAULT_TOOLS`, which carries `new_part`/`save_part` and would make the general lane
modeling-capable by default against the card's whole design.

Also: `test_harness_integrity` was checking `reviewer_exec._resolve_fn` (tier 1) while the real
dispatcher is `agent_turn._bind_tool` (three tiers) — it now uses `_bind_tool`, which is a
strictly stronger gate.

## 2026-08-04 — Sub-agent lifetimes: no timers, automatic spawn, lossless transcripts

Four Hunter decisions, one cutover. **(1) Spawn approval is gone**, not defaulted to yes:
`routing.yaml approval_policy`, `RoutingTable.approval_for_depth`, `ApprovalState`, the node
`approval` object, `DelegationTree.approve`, `_request_overseer_approval`,
`reconcile_registry_decision`/`await_registry_decision`, the `subagent_control`
`approve`/`deny` ops, and the ai_workspace Approve/Deny widgets + nav badge are all deleted.
`request_delegation` auto-approves at every depth and records `approved_ts`. **(2) No
lifetime timers**: `CHUDWARE_SUBAGENT_HOLD_MAX_S` and both 900 s caps are deleted; a pause
parks until resume or terminate. **(3) The watcher heartbeat became a controller-liveness
detector** — `note_watcher`/`watcher_active`/`WATCHER_TTL_S=30` -> `note_controller`/
`controller_alive`/`CONTROLLER_GRACE_S=120`, `controllers.json`. It no longer answers "should
I ask a human", it answers "could anyone ever release this hold": a pause whose GUI died
releases because the only party that could resume it is provably gone, and an unattended run
never parks at all. **(4) Sub-agent transcripts are now genuinely lossless.** Previously the
only model output that reached disk was `str(reply)[:4000]`; the real `messages` list was a
local that died when `run_turn` returned. `agent_turn._RawMirroredMessages` (a list subclass,
so all ~60 append sites are captured at one seam) mirrors every message verbatim into an
append-only JSONL (`chudware.subagent.raw/1`) at
`<Sessions>/<conversation_id>.subagents/<subagent_id>.jsonl`, with header/message/lifecycle/
compaction records and a restart-safe monotonic `seq`. `worker_rpc._agent_admin_turn` binds
the path from new `session_id`/`sessions_dir` params (composer.cpp -> run.cpp), so identity
never threads through delegation; `forkConversation` copies the sidecar dir and the session
prune removes it under the existing `s_pruneableSessionStems` guard.

Gotchas for the next person: the raw log is lossless *with respect to the conversation* —
tool output is still budget-capped on its way INTO the messages list, and the log honestly
records what the provider was actually sent. `bind_session` is a process-level global, so
tests must clear it (the registry suite's autouse fixture does). The turn boundary had ZERO
direct coverage before this; `test_subagent_turn_boundary.py` is new and drives real
`run_turn(subagent_id=...)` state, including a fake-clock test proving ten simulated hours
cannot resume a pause.

Files: `services/agent/{agent_turn,worker_rpc,subagent_registry,subagent_rpc,delegation,
mission_dispatch,routing}.py`, `routing.yaml`, `host/src/ui/ai/ai_workspace.cpp`,
`host/src/ui/copilot/{composer,conversations}.cpp`, `copilot_internal.h`,
`core/agent/run.cpp`, `contracts/{worker_rpc,advisor}.md`,
`docs/design/AGENT_DESIGN_PIPELINE.md`, `docs/contracts/fixtures/routing/*.json`, tests.
Verified: agent suite 3142->3169 passed (0 failed), `scripts/check.py` all 8 gates PASS,
`./build.sh` links, headless `CHUDWARE_SHOT=chat` and `workspaces` (8 captures incl.
`workspace_08_ai`) both exit 0, and a depth-2 delegation smoke landed 35 mirrored messages
in the conversation's sidecar where one truncated reply used to be the entire record.

## 2026-08-04 — T-W11 advisory bench + two verifier/guidance bugs it uncovered
Hunter authorized paid subscription quota for T-W11 (Pillar-20 capstone) with the
explicit instruction that the tokens buy *found gaps*, not a model leaderboard. Two
real defects fell out before a single paid token was spent, both found by mining
what already existed on disk. **(1) The tier was dropped on the `enable_tools`
tools-block re-render.** `agent_turn._apply_categories` replaces the entire §9.1
block mid-turn and called `render_tools_block()` without `tier=`, silently taking
`DEFAULT_TOOLS_BUDGET_TOKENS` (2500, the local-24k floor) instead of the turn's own
budget (frontier = 10000). Measured on `add_features`: 12792 chars of schema and
guidance at `tier=frontier` versus 335 untiered — 2.8% of the 11886-char authored
docstring, with the tool dropping out of the expansion pass entirely. It landed on
exactly the wrong turn, because `general_agent` starts without the modeling family
and can only reach `add_features` THROUGH `enable_tools`. In 59 genuine user runs
(`cache/runs/*/events.jsonl`, filtered to `run-`/`linear-`/`asm-` prefixes — the
skill's `^run-\d+$` filter is stale and misses the GUI's own linear lane),
`add_features` is 62 of 86 tool failures (72%) at a 23.2% overall failure rate, and
every observed failure class is documented in the guidance being discarded.
**(2) `verify_battery`'s FEA check graded nothing.** `_extract_score` scans its
scalar key list at the TOP level, but the sim tools return the scalars nested under
`summary`, so no key matched and it fell through to `1.0 if ok else 0.0` — "did the
solver run", not "did the part pass". An 80x50x4 aluminium plate at 50 kN solves
`ok=True` with `min_fos` 0.9944 (a FAILING margin) and scored a perfect 1.0; it now
scores 0.4972. That is fabricated success inside the check that gates mission
done-ness. `run_fea_static`'s docstring also advertised a flat return shape the tool
has never produced — the same misdirection that caused the bug.

New: `tools/flywheel/bench/benches/advisor_e2e.py` (registered in `runner.py`'s
`_BENCH_MODULES`) runs the bearing-mount-bracket mission through the baseline linear
lane and the advisory orchestrator-over-DAG lane, scores it with deterministic
checks only (bore fit via `fit_calc`, hole pattern via `query_geometry` `concavity ==
"bore"`, `is_manifold`, FEA `min_fos`), and emits a **gap ledger** bucketing every
observed tool failure into harness_defect / dialect_gap / guidance_gap / teaching.
`tools/flywheel/bench/spend_guard.py` reads live subscription utilization from the
OMP bridge's OAuth store (`<appdata>/omp-bridge-auth.db`, opened read-only+immutable)
and halts a matrix at an ABSOLUTE cap; an unreadable meter fail-safes to stop.

Gotchas for the next person. Live legs MUST hold a private daemon
(`services/mcp/private_hostd.private_daemon()`): `core_bridge` attaches to whatever
hostd is listening, and during one leg the shared daemon's open-doc count climbed
2 -> 15 from a concurrent pytest run and the leg's own target document was destroyed
mid-run. Doc-GC also retires idle documents — hostd logged
`doc-gc: retire doc_save(doc-cpp00000010) failed` against a live leg's own doc — so
the matrix pins `CHUDWARE_DOC_GC=0`. Matrix cells are SEED-MAJOR so a spend-cap stop
still leaves a balanced loop x tier block. `_bind_tool` bypasses `_reconcile_kwargs`
(which lives in `_dispatch_tool`), so probing dialect gaps through `_bind_tool`
manufactures false positives — `export_part(format=)` "reproduced" that way and is
in fact fixed. Likewise the `undo_denied` refusal DOES ship full recovery guidance in
its `hint` field; the event's `error_preview` clips it, which makes the corpus look
worse than reality. `BENCHES_DIR` is an `os.path` string, not a `Path`.

Filed `work/backlog/T-BL61.md`: a fillet inside `doc_apply_ops` SIGSEGVs hostd via
OCCT `ChFi3d_Builder::PerformFilletOnVertex` (coredump + full backtrace on the card).
`geom::blend_drop_failing` cannot contain it — OCCT segfaults rather than throwing,
and a catch block cannot catch a signal. No deterministic reproducer yet; a bare box
and a bored plate both survive every radius tried.

## 2026-08-04 — basic-modeling live sweep: silent selection bugs and unearned finishes

Broad "does the ordinary stuff work" pass over the baseline linear loop
(`mission_dispatch.run_linear_role_turn` / `general_agent`), Claude only
(sonnet-5 primary, opus-5 breadth, fable-5 spot check), seven ordinary modelling
requests per model against a private hostd. Nine fixes landed; every one is a
defect a live model actually hit, and each has a fail-then-pass regression test.

The load-bearing find is a whole CLASS: **`core/doc/picks.cpp::resolve_geom_query`
reads the query keys it knows and ignores the rest**, so a key the engine has no
concept of widens a query to every candidate instead of narrowing it. Measured on
a 50 mm cube with one bore: `{"of":"face","shape":"cylinder"}` (`shape` is the
spelling `query_geometry`'s own docstring advertises) matched all 7 faces, and
`add_features(fillet, refs.edges=[{"of":"edge","sid":"NOPE_NOT_A_SID"}])`
succeeded and rounded EVERY edge — ok:true, 22 faces added, 620 mm³ gone, no
error anywhere. `cad_common.normalize_geom_query` now canonicalises the alias keys
inside a query DICT (the flat-kwargs path always did), refuses an unknown key with
the real vocabulary, and refuses a query whose only non-domain keys came off a
readback. It is wired into `_read_query` (query_geometry / measure_between) and
into `_normalize_feature_refs` (every `add_features` ref). Both opus-5 and
sonnet-5 tripped it in production the same afternoon — on `convexity`,
`orientation`, `which`, `at` and `normal` — and recovered every time.

The sibling defect: a `sid` never selected anything. `_resolve_sid_refs` only
recognised a BARE sid string, so `refs.edges=[{"kind":"edge","sid":"E6b9abd"}]`
was rejected for a missing centroid — and the model answered by INVENTING
`centroid:[0,0,0]`, which was accepted and chamfered the rim at z=0 instead of the
one at z=100 the sid named. A sid is now read out of a selector or query dict too
and replaced with the engine's own live match, so the identity wins over anything
guessed beside it; `measure_between` and `query_geometry` resolve one the same way
(`cad_common.resolve_readback_selector`), and a stale sid gets the live roster.

Also: `_META_TOOLS_NOT_WORK` held only the capability/plan tools, so a turn made of
`enable_tools` + `get_part` on an empty document closed as `success=confirmed` with
nothing built; the workspace-describing readbacks joined the set (tools that can BE
the answer — measure/render/query/export — deliberately stayed out). `set_material`
is the VISUAL material and never touched the density `measure_part` computes mass
from, while `measure_part` refused `material=` with "use set_material instead" — an
aluminium-coloured bar reported a steel mass; `measure_part` now resolves a material
NAME, and both docstrings were corrected. One empty completion killed 2 of 8 legs
mid-build (only kimi-code resampled; everyone does now), and a `stopReason:"length"`
with no text is reported as truncation with the budget and effort named rather than
as "no completion" — that is why every `claude-fable-5` leg died in a second at the
default `reasoningEffort="high"`.

Gotchas. The spend guard read the bridge's WAL-mode auth store with `immutable=1`,
which skips the `-wal` file — it saw a long-expired access token, got HTTP 401 and
fail-safe-stopped every run; plain `mode=ro` with an immutable fallback. `component`
inside a query dict never scoped anything (`doc_query` reads only `body_id` before
handing the dict to `resolve_geom_query`) and a test had been asserting through it.
`benchmarks/simsuite/fea_static_cases.py:531` calls `doc_query` with
`{"of":"face","curved":True}` — `curved` is not a key, so that "inner bore" selection
is really every face; it bypasses the Python guard (direct `cb.call`) and is fixed in
the entry below, at the engine. `services/mcp/test_modeling_repertoire.py` is the free
canary: it drives
the whole ordinary repertoire through `_dispatch_tool` and asserts hand-computable
volumes — three of this session's findings were visible there before a token was
spent.

## 2026-08-04 — the dead query key, and a budget made of imaginary money

Two follow-ups from the sweep above, both "the code was lying about what it did".

**A geometry-query key the engine ignores is now an error.** `resolve_geom_query`
reads nine keys and silently drops everything else, so a predicate it has no concept
of does not narrow — it matches every candidate. Measured on an annulus (Ro=40,
Ri=20): `{"of":"face","curved":true}` returned all 4 faces, identical to a bare
`{"of":"face"}`. The Python guard added above only covers the MCP tool functions; a
direct `core_bridge`/C++ `doc_query` bypasses it. The check now lives in the engine at
the two EXTERNAL surfaces — `doc_query` (session.cpp) and `validate_geom_query`
(validate.cpp) — and deliberately NOT inside `resolve_geom_query`, whose 15+ internal
callers feed it synthesized queries and resolved selector objects that a strict check
would break. Vocabulary mirrors `cad_common.py`, including the readback fields
(`sid`/`centroid`/`normal`/…), because a `describe_part` match object is legitimately
pasted back into a query slot, and `doc_id`/`query`, because `libchudware.cpp`
dispatches `params.contains("query") ? params["query"] : params`.

Both riders of the dead key turned out NOT to be broken, which is why it survived. The
Lamé bore selection scanned all 4 faces and let a Python-side `|radius − Ri|` test do
the narrowing the query claimed to do — proven equivalent before touching it (old and
new select a byte-identical faceset: cylinder r=20, axis +Z, 310 points; the case
still solves at 1.97% of analytic `u_r(Ri)` against its own 8% self-fail threshold).
`test_curved_faceset_covers_cylinder_exactly` took `matches[0]` of an unnarrowed
3-face result and passed only because the barrel enumerates first. Same shape as the
`component` key above: a test asserting through a key that never did anything. So the
feared "the benchmark is pressurising the wrong faces" did NOT reproduce — the key was
dead weight (3 wasted `doc_face_points` round-trips) and one refactor from being wrong.

**The Eng. Firm dollar budget is gone.** It was fictional money.
`estimate_task_tokens` is a word count (`domain_overhead + words*1.4 + tier/2` in,
`tier_base + words*2` out), `tokens_to_usd` priced it at a hardcoded $3/1k, and
`spent_usd` never saw a real token — `run.cpp` did `spent_usd += task_bid_usd`,
accumulating the same estimates it checked against. On a subscription the number meant
nothing. Against the shipped $25 default: "design a mounting plate with four holes"
estimated $39.07, "two brackets that bolt together" $114.05, "route a PCB for an
ESP32" $117.08 — each returning `status:"failed", stop_reason:"budget_exceeded"`
BEFORE any task ran. Only a trivial request ("a simple washer", $18.79) got through.

Firm selection stays (the winner seeds role prompts and per-task provider config, and
a specialist wins on efficiency < 1); every dollar and every cost-based refusal goes —
`Bid::usd`, `within_budget`, `tokens_to_usd`, `budget_usd`, `spent_usd`,
`awarded_usd`, the mission-level and per-task aborts, the overspend check, the
`companyBudgetUsd` setting with its Settings row, and the `budget_exceeded` stop
reason. Bids rank in estimated TOKENS — a real unit of the thing estimated. The same
five requests now all run and each picks the right specialist. `budget_exceeded`
survives only in `core/solver`/`core/sketch`, an unrelated 10 ms solve deadline.

Not touched: `tools/flywheel/bench/spend_guard.py`. It is a dev-only gate on Hunter's
real subscription during live-model bench matrices, never in the product path, so it
never stopped a user session — and its false-UNREADABLE bug was the WAL fix above.
`services/agent/providers/token_usage.py` was checked and is pure accounting: it never
raises and nothing enforces a cap from it. `max_tool_calls`/step caps are loop bounds,
not token budgets, and are left alone.

## 2026-08-04 — standard agent + role sub-agent lane, live on Claude

Live sweep of the general lane (`agent_admin_turn` → `run_linear_role_turn`) with
claude-sonnet-5, deliberately NOT eng_firm. Whole session cost ~1% of the weekly
subscription (8% → 9%), because every defect below was reduced to a free repro before
anything was changed.

Inline lane is healthy: "40 mm cube, 10 mm through hole, tell me the volume" ran
`enable_tools → new_part → add_features → measure_part → verify_battery`, streamed 45
deltas, and reported 60858.4073464102 mm³ against an analytic 60858.4073465 — 9 s.f.

**A plan block swallowed the tool call after it.** Asked for two parts via sub-agents,
the model replied with a bare ```json {"task_list": …} fence THEN a second fence with
`{"tool":"delegate"}` for Part B. `_parse_directive` returned the first candidate that
merely parsed as a dict — the plan, which carries no `tool` — so Part B never ran and the
turn still closed `success=confirmed` while its own handoff read "Delegate Part B":
running. Fixed twice over: the parser now scans EVERY fence for an actionable directive
(tool call or done/handoff/status envelope), falling back to a non-directive object only
as a last resort so the mega-24c junk floor is unchanged; and `_normalize_directive`
folds the bare `{"task_list": {…}}` payload onto a real `task_list` call, since its inner
dict is already that tool's kwargs and the lane does grant the tool
(`_apply_session_preferences`, default preference "encourage"). Re-run: 17 calls instead
of 2, four task_list revisions tracking pending→running→done, a `feature_modeler`
sub-agent spawned and built Part A, both parts delivered at 18652.61 mm³ (exact).

**One sub-agent spent the entire delegation budget.** That re-run then exposed the next
layer: the second delegate returned "parent node … is in status 'revoked'". `accrue()`
charges a child's spend to every ancestor and revokes any whose remaining budget hits
zero (contract §12) — and the root grant was 50_000 tokens while ONE real sub-agent part
build costs ~100k (measured 101,772 for 8 completions; providers resend the whole context
every call). So the first child always revoked the root and every sibling delegation was
refused; multi-sub-agent work was structurally impossible. Reproduced offline for free.
The §12 mechanism is right and is untouched — the three tests asserting root revocation
still pass, and an explicit 50_000 grant still revokes on a 60_000 spend — the GRANT was
simply two orders of magnitude too small. Also `_delegate_actually_ran` counted that
refusal as real work (the envelope was `{"error": …, "status": "failed"}` with no
`executed` key), so a turn whose only call is a refused spawn could have closed
`confirmed`.

**doc_bbox measured the display mesh.** The inline run reported `bbox_mm: [40.0, 40.0,
40.012]` for a 40 mm cube. `BRepBndLib::AddOptimal` defaults `useTriangulation` to TRUE,
so the box came from the cached tessellation plus its deflection: a cylinder r5 h40 read
[10.0062156, 10.0093228, 40.012431] while its volume was exact to 1e-11 — x != y is the
tell. Not cosmetic: doc_bbox also supplies make_standard_part's placement datum.
`sim_bbox_diag` fixes the same problem with `BRepTools::Clean`, but that MUTATES the
shape and doc_bbox is on hostd's read-only RPC allowlist, so we ignore the mesh instead
(analytic bounds are exact for plane/cylinder/cone/sphere/torus) and `SetGap(0)` drops
the residual 2e-7. Now exact on box/cylinder/sphere/blind-hole/through-hole, asserted at
epsilon 1e-12 — deliberately tight, since a loose epsilon is what let this through.

Checked and deliberately NOT changed: `doc_apply_ops`'s `volume` is ~0.003–0.009% off the
analytic value, but `document.cpp` documents that as a profiled 3× speedup with error
≪0.1%, "display/stats only; mass eval still uses exact VolumeProperties", and
test_document.cpp already asserts it at epsilon 0.02. Real remaining nit, for a future
card: the `add_features` response calls that field `volume_mm3` with nothing marking it
approximate, so a model can report it as a measurement — the inline run's own
`add_features` said 60863.61 where the truth was 60858.41. Also seen once and left alone:
`interface_check` rejecting `other_doc_id` (dialect gap; the harness named it correctly
and the model recovered), and a child node that outspends its grant being labelled
`revoked` after finishing its work rather than `done` (audit-trail cosmetics).


## 2026-08-05 — one design is one document: version bumps resume instead of minting

Reported with a screenshot of nine tabs — `AI: ruger_lcp_iwb_holster_v3` … `_v11` — after
accepting a "Recover unsaved parts?" prompt on launch. Read off the user's live daemon
(`hostd-d01409dcb94e6cb5.sock`, `doc_list`): nine part documents, 2–6 features each,
successive iterations of ONE holster; `recovery_pending: 0` because the prompt had already
been accepted and every row opened as a tab.

No code produced the `_vN` suffix — the MODEL picks the name. `new_part`'s duplicate guard
is keyed on the NAME (`doc_new(resume_if_open)`, decided under the engine's mint lock), so
a model that bumps a version every iteration never collides and mints every time. Each
extra mint costs a tab, a `.step+` in the user's project folder, its own AI-VC delta chain
(`services/plm/ai_vc.py` keys chains by `doc_id`), its own auto-registered PLM item
(`services/plm/vault.py::_explode_project_into_shadow` rglobs every `*.step+` under the
project and registers any unknown path as a NEW item, not a revision), and — nothing closes
mission docs, and `host/src/app.cpp` deliberately only *detaches* un-adopted AI mirror tabs
on exit — its own row in the next launch's recovery prompt. That chain is the whole reported
symptom; the recovery flood is a consequence of the mint count, not a separate bug.

Fix at the one seam that mints: `_version_series_base` / `_docs_in_version_series` /
`_pick_best_doc` in `services/mcp/cad_common.py`, wired into `new_part`, its `add_features`
batch-head twin (`{"op":"new_part",…}`), and `new_assembly` (which had no resume contract at
all and would uniquify to `motor_mount2`). A trailing EXPLICIT, separated version token —
`_v4`, `-rev2`, ` gen3`, `_iter7`, `_mk3`, `_attempt2` — means "the next version of what I
already have": resume the most-built member of the series. A trailing bare NUMBER does not
(`PN-1235` is a different part from `PN-1234`), so the token is required. Checked AFTER the
path-stem match (a file the user actually opened is the stronger identification) and never
for the exact name asked for (that stays the engine's locked resume). Empty siblings are
never the resume target and get swept by `_discard_unbuilt_orphans`; `fresh=True` still
forces a deliberate variant alongside. Reply carries `resumed_by="version_series"` and a
note pointing at `plm_timeline`/`plm_rollback`.

Measured live against the real engine (`CHUDWARE_ENGINE_DAEMON=0`), replaying
holster → holster_v2 → holster_v3 → holster_v4 with a feature per round:

| | before | after |
|---|---|---|
| open documents / tabs | 4 | 1 |
| `.step+` in the project folder | 4 | 1 |
| AI-VC chains (version histories) | 4 | 1 |
| checkpoints in the surviving chain | 2 | 8 |

The model side too, since the harness guard should be the backstop and not the teacher:
`ONE DESIGN IS ONE DOCUMENT` in `services/agent/roles/model3d.yaml` +
`prompts/model3d.md` (YAML wins for the Python path, `.md` is what C++ `agent_run_v2`
reads — both need it), `roles/feature_modeler.yaml`, and the `new_part` prompt_note in
`services/agent/tool_guidance/cad.py` (regenerate `tool_panel_tables.h`/`.snapshot.json`
with `tools/gen_tool_panel_tables.py`). Those geometry roles previously had zero mention
of PLM versioning — the roles doing the iterating did not know history existed.

Two dead-code gaps found while tracing and deferred at the time — WI-10's
`refine_pass_limit`/`should_stop_refine` (never called by anything but their own unit
tests) and `test_mega28_terminal.py::_drive` (monkeypatching functions T-RF5 deleted, and
itself never called) — are both closed in the entry below.

## 2026-08-05 — terminal save sweep + the refine cap stops being dead code

**Why agent parts were "potentially lost".** hostd retires an abandoned doc to its
crash-recovery orphan tier when `path.empty() || dirty || isAutosavePath(path)`
(`core/hostd/server.cpp::gcCloseDoc`), and every orphan is offered through the GUI's
"Recover unsaved parts?" prompt. Nothing closes the docs a run builds — T-RF5 deleted
`services/agent/loop.py::_terminal_autosave_close_docs`, C++ never re-implemented it, and
`core/agent/run.cpp` only `doc_save`s the one doc a DAG node is on. Read off the user's
`hostd.log`: eight docs retired in the SAME second at the 30-min agent GC grace
(`CHUDWARE_DOC_GC_AGENT_GRACE_S`), matching the `id_remap` the daemon reported at hello.

**Save, do not close.** The deleted mechanism closed those docs; that is why it should not
come back as it was. A close fans out `doc.closed` and destroys the GUI mirror tab the user
browses after a run — the exact thing the long agent GC floor exists to protect.
`cad_common.save_touched_docs()` instead writes anything hostd would retire into
`CHUDWARE_PROJECT_SAVE_DIR`, so the doc stays open AND has nothing left to recover. Hooked
at `mission_dispatch.run_linear_role_turn` (every normal outcome — it has no try/except of
its own) and in a `finally` at `worker_rpc._agent_admin_turn` (the escaping exception);
idempotent, so running twice is free. Scope: docs this worker created/resumed
(`note_touched_doc`, called wherever `_CUR["doc_id"]` is set) plus open docs the engine
attributes to an `mcp`/`agent` client — a per-turn worker fork cannot remember an earlier
turn's doc, and those were the ones that flooded the prompt. `origin_kind=gui` is NEVER
touched: a dirty unsaved user document is precisely the work the prompt protects. Empty
docs are skipped (hostd purges those anyway, and a stub `.step+` is the clutter being
fixed), and with no project folder the whole sweep is a no-op because there is nowhere a
human would find the file.

This matters most for work that had NO save path at all: only `new_part` and `add_features`
call `_project_save`, so every agent-built **assembly** (`new_assembly` +
`asm_add_component` + `asm_mate`) and every `edit_feature` / `delete_feature` /
`set_material` reached hostd unsaved. Measured A/B on the real engine, one scripted
assembly turn:

| | before | after |
|---|---|---|
| assembly's backing file | `mcp_autosave/…-doc-cpp00000002.step+` (an appdata crash snapshot) | `<project>/rig_after.step+` |
| `gcCloseDoc` would retire it | **yes** → a "part potentially lost" row | **no** → plain close |

**WI-10 refine cap, wired.** `scaffolds.refine_pass_limit`/`should_stop_refine` had been
dead since they were written (only their own two unit tests called them), and the risk in
wiring them was the input: `stable_geometry_count` had no producer and no definition.
Counting successful `add_features` — the obvious reading, and the one a naive wiring would
pick — would cap a capability-A part at ONE FEATURE, because A's limit is 1. So a "refine
round" is defined as geometry re-worked AFTER the model itself declared the build finished:
`_GEOMETRY_SETTLERS` is exactly `verify_battery` / `save_part` / `export_part`. A first
build therefore cannot be counted at all, however many features it takes, and the cap is
structurally incapable of truncating construction. Mid-build inspection
(`describe_part`/`measure_part`/`render_part`) deliberately does not settle — models
interleave it constantly — and the WI-2 auto-verify inject never appears as the loop's
`tool` because it dispatches directly. New `refine_cap` stop_reason with its own
`error_detail_for_stop` branch: reusing `max_iters` would blame a tool-call budget that
never ran out. Tests in `test_scaffolds.py` include the regression control that matters — a
six-feature build under A with `describe_part` between every feature is NOT truncated —
plus A trips on the first re-work, B allows one, C/frontier never trip.

**Dead scaffolding removed — four files, not one.** `_drive` helpers monkeypatching
`loop._drive_phases_from_dag` / `loop._terminal_autosave_close_docs` (both deleted by
T-RF5) existed in `test_mega28_terminal.py`, `test_mega8_rolesurface.py`,
`test_mega3_fixes.py` and `test_mega7_fixes.py`. NONE of the four was ever called: 192
lines that read as coverage for a mechanism nothing exercised, and three of those files
listed the uncovered item in their own docstring's "Covers …" header. Each docstring now
says plainly which item is NOT covered and why.

`test_mega21_fixes.py::test_loop_deliver_gate_blocks_zero_mech` went with them, for a worse
reason: it could not fail. It monkeypatched `mech_deliver_gate` to return
`allow_deliver=False`, re-implemented the status mapping inside its own body
(`status = "blocked"`), then asserted `status == "blocked"` — while importing `loop`,
`PhaseMachine`, a graph, a `dag_result` and a `fake_phase_completion` it never used. The
real gate is covered three times in the same file plus once in `test_mega22_fixes.py`.

`deliver_partial` now provably has NO producer: `grep -rn deliver_partial services/ core/`
returns comments, docstrings and tests only. The mission-terminal vocabulary
(`deliver_partial` / `mission_complete` / `needs_correction`) died with the Python loop and
C++ never reimplemented it — `core/agent/machine.cpp` treats only pass/fail/skip/cancelled
as terminal. Whoever owns mission terminals should decide whether that classification comes
back in C++ or is retired from the docs; it is currently asserted on neither side.
## 2026-08-05 — a revision is not a name, a budget is not a leash, a lint that read nothing

Second pass on run `run-12211675717239` (`ruger_lcp380_iwb_holster`), driven by the
event log rather than the GUI cards. Six defects, three commits: `5841479e` (Python
harness), `a462d93f` (core/hostd), `ae965af5` (GUI + scripts).

**Version tokens never reach a document name.** The 2026-08-05 series-resume work made
an ITERATION resume, but nothing stopped the model coining `holster_v1` on the very
first call — and that string became the tab title, the project `.step+` filename and,
through `vault.py`'s rglob auto-registration, the PLM item path. New
`cad_common._strip_version_suffix` (the series base, else the name) now normalizes the
payload of all three `doc_new` mint sites: `new_part`, the `add_features`
`{"op":"new_part"}` head, `new_assembly`. The resume/series lookups still see the RAW
requested name, and the strip is SKIPPED when a document of that exact name is already
open — that call is the engine's own `resume_if_open` match and it keys on the name
verbatim, so stripping there hands `doc_new` a name nothing is open under and the
"resume" mints a duplicate. `test_exact_name_match_still_goes_through_the_engine_mint_lock`
caught that in the full suite before it shipped; two twins now cover the batch head and
`new_assembly`. `core/agent/run.cpp`'s `sanitize_part_name` (the mission lane's own,
independent naming surface) mirrors the same rule token for token — it moved to
`run_stop.cpp` so `test_agent` can link it without pulling `doc::`/OCCT. Accepted cost,
documented at both definitions: `Intake_Manifold_V8` normalizes to `Intake_Manifold`. A
second, divergent definition of "version token" would be worse.

**`plm_item_add` was a four-call dead end.** Its `path` is the PROJECT ROOT while every
sibling tool's `path` is the target; `kind` had no synonym for `doc_type`; each error
revealed only the next problem; and once the shape was finally right, `Vault.add_item`
hard-blocks `.step+`/`.ecad+` because `_explode_project_into_shadow` auto-registers
containers on every save — a call that could never succeed, checked LAST. Now: the
container rejection runs FIRST and names `plm_checkpoint`, a file passed as `path` gets a
hint quoting the whole corrected call, re-registering an already-tracked non-container is
idempotent (`already_tracked`), `kind`→`doc_type` is in `TOOL_ARG_SYNONYMS`, and both
`general_agent.yaml` and `project_organizer.yaml` stop telling the model to register
design docs by hand. The per-tool `prompt_note` had been correct all along; the role card
outranked it.

**Main chat has no tool-call budget.** `LINEAR_ROLE_MAX_TOOL_CALLS = 30` hard-broke the
loop at `agent_turn.py`'s single enforcement site, one dispatch before the
`plm_checkpoint` the model had already composed, and the salvage block returned
`stop_reason=confirmed` for that forced stop — a fabricated success. Main chat
(`subagent_id is None`) now has only a runaway backstop; the budget is a SUBAGENT soft
warning, injected once through the same nudge idiom the loop already uses for half a
dozen other conditions, with a separate hard ceiling. Configurable: config.json
`agent.subagent_tool_call_budget`, `0` = derive from the token grant, read by
`config.get_subagent_tool_call_budget()` and surfaced by a Settings slider that replaced
the dead `max_iters` knob (a GUI control persisting a key no Python has ever read).
Forced stops report honestly.

**Delegated subagents were truncating at 2048 tokens.** `_run_subagent_turn` never passed
`max_tokens=`, so every delegate fell to `agent_turn`'s hardcoded default — the
`feature_modeler` in this run burned its whole 60,000-token grant on two `reply_truncated`
turns with ZERO tool calls, then the parent did the entire build itself under its own
30-call ceiling. The parent lane got `LINEAR_ROLE_MAX_OUTPUT_TOKENS = 8192` on 2026-07-28;
the delegate path never did. It does now. Highest-severity item in the autopsy by wasted
budget.

**The agent's documents now exist before its first feature.** `core/agent/run.cpp` mints
and mutates with direct in-process `doc::doc_new`/`doc::doc_apply_ops` calls that never
re-enter `Server::dispatch`, so `DocRegistry` never saw them: `postCall` fires once, after
the whole `agent_run_v2` returns. Assemblies, sub-parts and reverse-engineering documents
did not exist for any client until the run ended, and features landed in one end-of-run
resync. New `chudware_set_doc_publish_sink` (appended to the C ABI, dlsym'd, missing-symbol
tolerant) mirrors `chudware_set_event_sink`: `Server::dispatch` arms it for `agent_*`,
`run.cpp` publishes after each of the 3 `doc_new` and 2 `doc_apply_ops` sites outside the
per-document lock, and the callback drives the SAME `registry_.postCall` Path A uses.
`internal:true` propagates, so the preflight shadow clone stays silent. No GUI change was
needed — `openMirrorTab`/`applyChangedActive` were already gate-free, and an empty document
already draws its origin planes (`renderer.cpp` `planeQuad`'s `kEmptySceneRadius`
fallback). **Path A was measured, not assumed**: a real MCP `new_part` against a real
daemon with a subscribed spectator gave `doc.opened` at mint in 0.7 ms with a revision-0
snapshot, then `doc.changed` at the first `add_features`. It was left alone.

**Settings drew combos three ways and its lint read zero files.** `ImGui::Combo`,
hand-rolled `BeginCombo` loops and `settingsPopupRowCombo` coexisted; none called
`SetNextWindowSizeConstraints`, so popups grew wider than their closed box, and
`settingsManualRowSelectable`'s `ImMax(widthToInnerRight, textWidth)` could widen them
further on the next frame. Every combo now goes through one begin/end pair that pins
min==max width (the composer model-picker pattern), pushes zero popup
`WindowPadding`/`ItemSpacing` so the wash reaches the edge, and rounds the first/last row
— the treatment `copilot/ui_common.cpp`'s `chatCopyContext` already had. Zero
`ImGui::Combo(` calls remain in the file. Separately, `scripts/gui_lint.py`'s
`DEFAULT_ROOTS` pointed at `ui/imgui/src`, retired and absent: both gui_lint gates in
`scripts/check.py` had been walking ZERO files and reporting green. They now lint
`host/src` + `ui/viewport` (279 files), the dead ui/imgui SDF branch is gone, and the
newly-live theme rule immediately found a real bug (`wasm_boot.cpp` passing a hardcoded
`1.0f` to `applyTheme` instead of the GLFW content scale). 18 pre-existing icon findings
are baselined in `scripts/gui_lint_baseline.json`.

**Deferred, not done.** `services/plm/worker_rpc.py` has its own empty-target hint for
`plm_item_add` that still reads the old way (the `Vault` layer beneath it does carry the
new container behavior). `agent_turn`'s bare 2048 completion default is still reachable
from `services/dag/node_runner.py` when no tier profile exists. A `gui_lint --rule scale`
gate was deliberately NOT wired into `gate.sh`: a new gate starting life with 110
baselined findings is theater, and the baseline file makes manual runs ratchet anyway.

## 2026-08-05 — second pass: the split thinking box, the discarded stream, and three tail-of-run dead ends

Driven by a live headless replay of the user's exact request (`agent_admin_turn`,
kimi-code/k3-256k, 539 s, 26 tool calls, `stop=confirmed`) plus the raw 227-record event
log of the run that failed on his machine. Commits `42d33c75`, `08a82d1c`.

**One reasoning stream rendered as four out-of-order boxes.** `_AssistantDeltaCoalescer`
held independent `text`/`thinking` tails and drained them only at turn exit, text first,
so a tail crossed channel switches and non-delta events and glued itself to the NEXT
completion's opening — the seams `batch them.The user`, `saved file.The harness` and
`guide.Stop` are in the log verbatim. `core/agent/run.cpp`'s coalescer had drifted into
the identical bug independently; both now track the active channel, flush on switch,
drain the active channel first, and NAME EACH OTHER in comments so the mirror is not
rediscovered a third time. `feed.cpp` compounded it by treating every hidden
text-channel flush as a visible thinking boundary and by not closing a live thought
before a context chip. Replaying all 227 records under the corrected rules gives
context@2, thinking@3-194, context@204, thinking@205-226 — two boxes, split only at the
one real chip.

**220 deltas then no completion killed a turn.** The user's run streamed 202 thinking +
18 text deltas and died `provider_error` with nothing salvaged; the same fault reproduced
headlessly in a delegated child as `omp-bridge kimi-code: endpoint returned no
completion`. A resample existed but only inspected the FINAL result's text/thinking —
fragments went to the display sink and were never retained, so paid output was discarded
even when the predicate fired; and a stream cut before its final response line raised
`BridgeUnavailable` straight past the loop. `complete_ex` now retains per-attempt
fragments privately (still forwarding each to the display sink exactly once — the
display contract that forbids concatenating deltas into the reply is untouched) and
recovers streamed text, or a validated fenced directive from streamed reasoning, at zero
extra provider cost. An empty `length` reply carrying `reasoningEffort` retries once
without the effort; the code had DOCUMENTED that the same call succeeds without it and
never acted on it. Two-attempt bound unchanged, 400/401/403 still never resampled, and
an unrecoverable failure now distinguishes "final response contained no text or thinking"
from "stream ended before the final response line" all the way to the GUI card.

**`delegate` failed with the brief as its own error message.** The spawn had succeeded
and the child really ran; mission_dispatch merged the failed child result after the spawn
record with no top-level error, so the model, the GUI and the ledger's ~999-char result
clip all showed the brief. Success is now explicitly `ok:true`, refusals carry a stable
`reason`, and a failed child's stop reason and provider detail are promoted into a
top-level `error` ordered FIRST so it survives the clip.

**Three tail-of-run PLM dead ends, one root cause.** `plm_open()` bare, then
`plm_item_add(path=<a file>)`, now `plm_checkpoint()` bare — every `plm_*` tool demanded
a PROJECT ROOT the model has no way to know, at the exact moment a clean build wants to
record itself. 19 vault-backed tools now default `path` from `CHUDWARE_PROJECT_DIR` →
`CHUDWARE_FS_ROOT` → `project_context.resolve_project_root()`, explicit argument still
authoritative, named `project_root_unavailable` when nothing resolves. A released item's
rejection now quotes the exact recovery call rather than stating a fact with no verb;
checkpoint deliberately does not auto-reopen a release.

**A bare `save_part` wrote to /tmp** — and `chudware_mcp_*` basenames are exactly what
the artifact allowlist drops as internal scratch, so the save was misplaced AND invisible
and the model had to notice and re-call. `save_part`/`export_part` now default to
`CHUDWARE_PROJECT_SAVE_DIR`, keeping the temp fallback only for headless external MCP
clients with no project.

**Measured and NOT fixed: there is no first-token latency regression.** A live A/B with
the pre-session tree reconstructed under `scratch/` (no checkout, shared tree) gave 4.648 s
to first delta at HEAD vs 4.837 s at `c8b8e3a3`. Worker import is 105 ms including spawn
at both revisions. This session added ~66 tokens to the assembled prefix (25593 → 25857
chars); the tools block and pack menu did not grow. The user's slow first turn was a
one-time prompt-cache miss: the failing trace reports `cache_read=0`, and a controlled
probe measured 8.139 s cold vs 5.027 s on the immediate repeat (+3.1 s), recurring only
when the prompt prefix changes again or the provider cache expires. Removing correct PLM
guidance to preserve an old cache key would trade correctness for a one-time warmup, so
nothing was changed. Noted for whoever optimizes later: ~2.08 s of the interval is local
pre-provider work — tool-schema render 365-388 ms, project-memory recall 297-336 ms, and
~1.2 s of first-use imports / delegation-and-skills setup / resolver construction, most
of which is per-process rather than per-turn.

**Deferred.** The ledger clips every tool result at ~999 chars, which is why the delegate
failure could not be diagnosed from disk at all; delegate now front-loads its cause to
survive that, but the clip itself is untouched observability debt.

---

## Geometry emphasis: a real close-up camera instead of a 32x32 crop (2026-08-06)

**The defect, measured.** Every close-up an AI agent could get — `inspect_feature`,
`inspect_part`, `look_around`, `render_part(target=)`, `render_views(target=)`,
`measure` — was a whole-part render at 512x400 followed by a pixel crop and a bicubic
upscale, because `doc_render_view` could only auto-fit the entire model. A Ø5 hole in a
120x80x12 plate came back as a **32x32 pixel crop blown up 16x**: a white blob. Worse, the
emphasis overlay projected its paint through the *uncropped* whole-part frame while painting
onto the *cropped* image, so on every framed render the target colour landed somewhere else
entirely — in the reference case, a red dot on the blob's rim. And the "auto" camera was the
mean face normal with no occlusion test, which for a bore is its axis, so the ideal view of a
hole was chosen to be a stare straight down it, from underneath.

**Engine (`core/doc/session.cpp`).** `render_view_to_rgb` takes an optional `RenderFocus`
(world AABB + `margin` + `clip_near`) and an optional `RenderFrame*` out-param.  With a focus
the projection fits THAT box — no 0.85 auto-fit shrink, degenerate axes widened — so a
close-up is rendered, not magnified; `clip_near` drops fragments in front of the box, a
deterministic cutaway that sees into a bore without a section operation.  `doc_render_view`
exposes `width`/`height`/`focus` in the object form of `view`, heads a focused image with the
FRAMED dims rather than the whole part's, and echoes `frame` (centre/right/up/dir/scale/cu/cv/
size/`clip_depth`).  Two live bugs fixed alongside: `annotate` was accepted by every caller
and silently ignored (now plumbed through `libchudware.cpp`), and a `RenderLabel` whose anchor
projects off-frame is no longer drawn — that is where the giant stray "R" on close-up tiles
came from.  `min_painted` drops to 1 for a focused render; the whole-part floor called a
legitimate cutaway a failed render.  Pinned by six new cases in `core/tests/test_render_view.cpp`.

**Camera (`services/mcp/cad_viewpoint.py`, new).** Every direction in a fixed lattice — both
hemispheres around the target normal at 20/35/55/70 degrees, the 6 principal axes, the 8 isos
— is rasterized and MEASURED: unoccluded target pixels, share of frame, area-weighted
foreshortening.  Visibility **gates** (`min(visible_frac/0.5, 1)`); ranking on raw visible area
is what picked the bore-axis stare and the flat orthographic section, because those expose the
most pixels.  Three small terms then choose among the views that can see it: a bore-axis
penalty, a principal-axis ("flat 2-D, no depth cue") penalty, and a slight preference for a
camera above the part.  Separation between multi-view picks is UNDIRECTED, so an antipodal
camera is rejected too — front and back of one target are mirror images.  Asking for more views
than the separation admits returns fewer, never a padded duplicate.  **The scorer must be told
`clip_near` whenever the caller will render cut**: score an internal target uncut and every
oblique camera measures as 80% occluded while the useless axial one measures clear.

**Overlay (`cad_common`).** `_apply_emphasis_overlay` and `_project_match_pixels` are DELETED.
Emphasis is now a mode of the one overlay engine, `_annotate_render(emphasis=...)`, reusing the
same depth-tested visible-face masks `highlight` already used: the target is painted in its
palette colour modulated by the pixel's own luminance (a flat fill turns a bore into a
sticker), context is desaturated, the boundary outlined, each sid leader-labelled, and
`info["emphasis"]` reports MEASURED coverage plus which subshapes this camera cannot see.
`_mesh_depth_buffer` applies the engine's echoed `clip_depth` — without that the cut-away
material still wins the overlay's z-test and the feature the cutaway exists to reveal reads as
hidden (this bit once during the work; the symptom is "0.0% target" on a picture that plainly
shows it).  `_tile_pngs_grid` draws its label in a band above each tile instead of stamping it
over the engine's baked header, which had made both unreadable.

**Tools.** `inspect_feature` returns a deterministic multi-panel sheet — HERO (idealized
camera), ALT (>=75 degrees away), CUTAWAY-companion (for an internal target HERO/ALT are cut
and the third panel is AS-BUILT uncut; otherwise a third angle), and CONTEXT (whole part, target
highlighted, so the close-ups are placeable).  `inspect_part` defaults `emphasize=True` and
takes `cutaway=None|True|False`.  `look_around` ranks idealized angles by default
(`ranked=False` keeps the even orbit) and reports coverage per tile.  `render_views(target=)`
frames and cuts every tile: on the reference hole it went from 1 of 7 tiles showing the target
to 5 of 7.

**Gotchas for whoever is next.** (1) Whenever you add a close-up path, pass the ENGINE's echoed
frame to `_annotate_render`; re-deriving it in Python is a guess that is meaningless on a
focused render.  (2) Scoring and rendering must agree about `clip_near` or the ranking optimises
a picture nobody sees.  (3) `_target_is_internal` decides the cutaway; a mis-classified target
just gets a less useful (never a wrong) picture.  (4) `_closeup` builds the palette itself and
reports it even when the tessellation is missing, because the caption's `palette_json` block is
the machine-readable half of the contract.

## 2026-08-06 — a brand-new AI part is no longer thrown away the moment you look elsewhere
Reported as "I can't open or select the part the AI just made until it adds the first feature."
Root cause was `AppState::switchToTab`'s empty-AI-mirror sweep (`host/src/core/doc_tabs.cpp`):
it auto-closed the tab the user was LEAVING when that tab was an AI mirror with `agentDone` and
no agent edit stamp. `agentDone` is set by `doc.closed(reason=last_writer_detached)`, which hostd
emits at the END OF EVERY TURN (the general lane spawns a fresh worker per turn) — so a part the
agent had just minted and not yet built into matched within seconds of being created. Glancing at
another tab detached the GUI's only view of it, and because `detach_doc` on a read-only attach
reports no surviving-close note, `dismissedMirrorDocs` stayed empty and the part vanished from the
tab strip AND from Artifacts ▸ Agent Documents; only the agent's first `add_features` (a
`doc.changed`) re-revealed the mirror. Fix: the sweep is gone and `switchToTabCore` merged back
into `switchToTab` (one function, no auto-close anywhere in the switch path); `agentDone` is now
documented as display-only ("no writer attached right now", not "abandoned"). Reproduced and
verified against an isolated hostd + headless GUI on Xvfb (`CHUDWARE_INSTANCE_KEY=guiprobe*`,
`CHUDWARE_LOG_FILE`): before — mint an empty agent doc, switch away, `[tabs] auto-closing empty AI
mirror on switch-away` and the tab is gone, coming back only after one `doc_apply_ops`; after —
the tab survives the switch-away, re-activates on click (`path=gpu-cache`), takes live geometry
when the agent finally writes, and manual X-close still closes it. Files: `host/src/core/
doc_tabs.cpp`, `host/src/core/app_state.h`, plus comment-only follow-ups in `host/src/core/
engine_events.cpp`, `host/src/ui/viewport_panel.cpp`, `core/hostd/doc_registry.cpp`.

## 2026-08-07 — Manufacture becomes one native-viewport CAM workspace

The partial CAM overhaul still owned a bottom-docked `ImGui::Begin("CAM")` editor and a duplicate
2-D backplot even though toolpaths had begun drawing over the part. That split shrank the central
view, duplicated preview state, and left several correctness holes: edits could keep stale
toolpaths/simulation/G-code visible; operation fields never loaded the selected operation's
serialized values; extraction followed whichever tab was active later; loaded jobs could inherit
the previous job's paths/source/post; manual Simulate/Post could auto-generate core paths without
refreshing the viewport; playback inferred start time from the previous segment of the same move
class; a new simulation at frame zero could reuse the prior aux mesh; and CAM overlays leaked into
non-Manufacture workspaces.

`host/src/ui/cam/{cam_panel,cam_mirror}.{cpp,h}` now make the pinned left column the complete
control surface: Explorer / Job Manager / NC Output, an identity-keyed operation inspector,
simulation evidence, post/audit/G-code, and a viewport-local telemetry/transport HUD. Source Part
identity is pinned, mutations use the mirrored revision and invalidate derived artifacts, job post
flavor is aligned, machine rates reset on reload, paths carry true global start/end times, arc time
uses arc/helical length, and stock/direction geometry is cached. `host/src/ui/viewport_panel.cpp`
and `host/src/render/overlays.{cpp,h}` draw timed glow/core path ranges, direction glyphs, cached
stock, a persistent native tool marker, and remnant stock inside the existing renderer FBO; the
aux key includes simulation generation and all CAM geometry is Manufacture-gated.
`host/src/ui/layout.{cpp,h}`, `host/src/ui/metrics.h`, and `host/src/app.cpp` delete the CAM dock
node/top clamp and roll the ImGui layout schema to v6; `host/src/ui/scenarios.cpp` now captures the
real integrated layout. Regression coverage is `host/tests/test_cam_mirror.cpp`. Verified with a
clean `./build.sh`, `test_cam_mirror PASS`, theme lint at zero findings, and the real
`CHUDWARE_SHOT=cam` flow: part revision 1, CAM doc `cam1`, 3 operations, 394 NC lines, framed
simulation loaded, 95.5 s playback, and a successful 1340x838 capture.

## 2026-08-07 — harness policy audit: one effective prompt, grant, and child-lane contract

A parallel read-only audit of rules, skills, prompts, dispatch, budgets, guardrails, and
lifecycle seams found several cross-layer contracts that were individually plausible but
collectively false. Explicit `tools: []` collapsed into unrestricted access; DAG children
received the main shell floor while missing their read/glob/grep floor and child identity;
mission-local handlers bypassed per-tool disable/default/timeout policy; tools were rendered
before their final meta grants; Settings' output-token and system-prompt controls were dead on
the online lane; mission and native prompts with the same id overwrote one shared override
file; and blocked/escalated role handoffs were reported as confirmed success.

The runtime now preserves `None` versus `[]`, assembles lane-specific floors before both schema
rendering and dispatch, routes every handler through `_dispatch_tool`, validates
`enable_tools(categories=[...])`, advertises effective parameter defaults, normalizes child
tier/capability policy, and maps reviewer/model seats through one helper. Live, preview, and
prompt-cache assembly share the main-chat loadout and effective global/role prompt. Mission
overrides use `<id>.mission.md`; native C++ keeps `<id>.md`. Task-list Require blocks the first
non-task-list call while Encourage remains a later nudge, and role/phase skill allowlists now
reach both initial and tool-trigger activation. Trigger rules were narrowed for security
scans, electronics datasheets, mold release angles, and generic part search; newly created
user context packs are detected without process restart.

Regression coverage exercises main/delegate/DAG grant parity, explicit-empty denial, final
tool schemas, per-tool mission policy, malformed category calls, blocked/escalated verdicts,
reviewer no-tool-one-shot refusal, prompt override isolation, global prompt/output settings,
skill role gates, cross-process pack creation, and xAI/OpenRouter tier normalization. The
focused contract gate passed 317 tests; an actual prompt-preview smoke rendered 24 effective
tools in 22,660 characters with no fictional ledger/stage blocks.

## 2026-08-07 — Parts Library live sourcing reliability

The Parts Library scraper now discovers the shipped lowercase CEF host, bounds live vendor
navigation three-wide, keeps transient failures out of the vendor-result cache, and reports failed
vendors with correct attribution instead of a false done state. Ten drifted vendor adapters were
live-refreshed against their current routes and rendered markup; HIWIN uses its cached public
sitemap and Future Electronics uses same-origin autocomplete where the normal result surface is
not capturable. A full uncached sweep exercised all 59 adapters: 42 returned usable results, 16
honestly reported login/challenge/bot-wall degradation, and the sole parser miss was fixed and
rechecked. The standard-parts benchmark indexed 3,184 records in 61.8 ms cold / 5.9 ms warm,
searched in 0.66 ms mean with MRR 1.0 and 20/20 hits, and the isolated browser/parts gate passed
801 tests with 81 live-only skips. A real nine-vendor bearing search painted results in 4.4 s cold
and 52.6 ms warm, while the Parts Library headless workspace smoke produced a 1340x838 capture.

## 2026-08-07 — Webservice settings, provider auth, and rollout hardening

The browser-native service now redirects `/app` to canonical `/app/`, accepts the flat v2 chat
store that the WASM client actually persists, serializes/coalesces IDBFS writes, and no longer
uploads recents on unrelated settings changes. Provider Settings uses generation-guarded
list/detail/key/OAuth callbacks, a shell-free browser URL opener, cancellable immediate OAuth
starts, cryptorandom retained handles, device/manual interaction metadata, and honest retry/error
states. A real isolated gateway/hostd browser smoke reached Kimi's device authorization URL with a
UUID handle and no false manual-input affordance. Provider management is admin-only at the engine
boundary; standard provider catalogue responses omit account identity, list-model credential
injection accepts only canonical endpoints, and concurrent engine slots are reserved atomically.

Session start/stop/reap/adoption is serialized per account with incarnation checks, fresh health
probes, and full-email hashed names. Deployment now promotes an immutable joint native/WASM/Bun
release, drains from authenticated bridge telemetry, fails closed on missing telemetry or
readiness, and keeps gateway/WebSocket size limits aligned. Verification covered the 272-test
gateway suite (272 passed, 4 skipped), a native application build, the completed WASM target, the
`/app` redirect plus relative asset fetch, exact v2 chat round-trip, role-aware provider privacy,
admin provider enumeration, standard-user provider denial, and a live Kimi begin/poll/cancel flow
against an isolated credential store.

## 2026-08-07 — Full simulation-suite accuracy and workflow audit

The complete simulation surface was exercised from native kernels through MCP and desktop
workflows. Static/modal/thermal/fatigue FEA setup and tet10 accounting, assembly handoff,
hydraulics/piping, SPICE/ECAD jobs, CHT, motion dynamics, cancellation, caching, and structured
failure envelopes were corrected and cross-checked against independent analytic anchors. The CFD
path now exports a real STL, samples pre-bounce solid-link momentum, time-averages the settled
force, requires continuity and positive passive-body drag, invalidates stale solver caches, and
discloses its $\tau=0.55$ reduced-Re stability model. OpenFOAM v2412 flat force tables are parsed
alongside legacy tables; the live coarse sphere returned $C_d=0.329$ at $Re=10{,}000$, inside the
accepted steady-RANS band.

The Simulation Manager no longer presents numeric-only LBM output as fake temperature/plot data
or drops the editable model when no contour exists. Its flow smoke reaches a visible force and
coefficient table with the physical/effective Reynolds disclosure. The motion smoke now authors a
grounded two-body assembly in one revision-safe operation, drives a resolved distance mate across
24 collision-free frames, and visibly advances playback. Final reruns completed with 168 Python
simulation tests passed (5 optional skips), 173 end-to-end MCP workflow tests passed, the 28-test
EM suite passing against the installed openEMS full-wave backend, all 15 focused native targets
passed, and the live audit at 42/43 executed with its heavy openEMS leg intentionally skipped and
zero broken findings.

## 2026-08-08 — Manufacturing overhaul, phase P1/P2 foundation

The Manufacture overhaul (real machine control, deterministic physical twin, additive with an
internally modifiable slicer, probing/metrology, a home-grown predictive perception model, and the
breadth tracks) now has a persisted design corpus under `docs/manufacturing/` — `README.md` is the
index, `01_definition_of_done.md` the 20 acceptance criteria, `10_gateway.md` the schema authority
that owns every shared type and the `MFG-*` rule ids the other documents cite.

Landed this phase. `services/agent/config.py` gained `DEFAULT_OFF_TOOL_FAMILIES`, so the
`manufacture` family resolves OFF by omission — with no config, with `role_tools` present and
silent, with a role listing it, and even before the family joins `DEFAULT_TOOL_FAMILIES` (that
entry lands with the first Manufacture tool, because `test_registration_catalog` forbids a phantom
family). `contracts/manufacturing_gateway.md` is the new canonical contract; `contracts/worker_rpc.md`
and `contracts/events.md` took append-only sections plus the `observer` client kind and the
subscription-lane amendment. `core/device/{device_id,controller_snapshot,capability,transport}.h`
carry identity minting, `MachineFingerprint`, the canonical `DeviceState`/`RunState` strings with a
transition validator that refuses transparent resume after reset/alarm/hard-limit/disconnect, the
capability truth vocabulary, and a read-only/command transport split with no I/O. `core/manufacturing/`
adds the sole `FreshValue<T>` codec (null-unless-ok, gap→unknown, `ttl_ms:null` for non-decaying
durable rows), a single-writer append-only JSONL journal that is the only source of durable `seq`
(torn-tail recovery, monotonic reopen, replay), a total durable-result-kind registry over all ten
domains including each `<domain>.capability`, and the idempotent `at_seq`-scoped falsified/superseded
engine. `core/device/grbl_protocol.{h,cpp}` is a pure syscall-free Grbl 1.1 codec (status fields,
full error/alarm tables, setting maps, realtime bytes, character-counting RX accounting) with raw-line
retention and explicit refusal on malformed input.

Python side: `services/manufacturing/slicer/**` is the arms-length PrusaSlicer sidecar seam —
request/result/provenance, deterministic profile hashing, an AGPL corresponding-source manifest
verifier, and `fixtures/mfg/print/fake_slicer.py` so tests never need the real binary;
`services/manufacturing/monitor/**` parses MTConnect read-only into canonical rows with
UNAVAILABLE/gap→unknown, falsify + retain-as-last-ok conditions, and a structural test proving no
command path exists; `services/perception/**` + `training/perception/**` implement the
Forecast-to-Compress MLDP — numpy reference forward pass, optional Torch parity, multi-horizon and
uncertainty heads, variance-normalized surprise, advisory-only evidence packets, dataset manifests
that reject missing consent/license/redaction fields, and a fixed-seed CPU train/eval path.

Gates: `test_mfg_foundation` 329 assertions and `test_grbl_protocol` 16,011 assertions pass;
`pytest services/manufacturing services/perception training/perception` is 45 passed / 1 skipped
(optional `onnx` absent); `scripts/check.py` all 8 gates PASS.

Deferred by design, not omission: no transport opens a device, no arbiter/lease/approval path exists
yet, no MCP tool is registered, and nothing advertises physical `execute` — that is phases P3–P7,
and P7 is hardware-attended.

## 2026-08-08 — Manufacturing overhaul, phase P3 authorization path + domain cores

The hardware-free device lane and the deterministic authorization core landed. `core/device/grbl_adapter.{h,cpp}`
binds the pure codec to `ControllerSnapshot`: identify into a `MachineFingerprint`, character-counted
program streaming, hold/abort/reset as protocol operations, alarms and hard limits as terminal
discontinuities that forbid transparent resume, lane-epoch bump on reopen, poll gaps painting
`unknown` instead of last-good, and capability evidence capped at `parse`/`transport`.
`core/device/emulator_transport.{h,cpp}` is a deterministic in-process `ITransport` speaking real Grbl
1.1 with injectable alarm/hard-limit/door/disconnect/garbage/overflow faults; its endpoint is
non-physical, so everything it produces is `src:"simulated"` and can never satisfy an execute-side gate.

`core/manufacturing/{tier0,policy,gates,approval,lease,arbiter}.{h,cpp}` is the safety heart:
artifact-dependent, machine-class-scoped Tier-0 construction refusals that name the missing capability
or artifact; commissioning-owned machine-class policy (attendance, spindle authority requiring
commanded *and* observed feedback, the `exercisable_by` ceiling); two-stage gates where Stage 1 admits
only fresh probed evidence or a bound procedural fact and is never sufficient, and Stage 2 is
`pass|fail|not_applicable` with durable named waivers and a seq-stamped report; `ApprovalBinding` with
a one-shot nonce, `max_starts=1`, expiry and fingerprint-change falsification; an exclusive per-device
command lease; and the arbiter as the ONLY validator of approval, with an injected clock and journal,
recorded initiator provenance, and no path that resumes after a discontinuity.

`core/twin/**` is the deterministic engineering twin — integer ticks only, snapshot/restore with a
determinism hash, 3-axis kinematics driven by the SHARED Grbl codec so program semantics cannot drift
from the real lane, heightfield material removal, typed collision and fault events, virtual sensors and
cameras that are always `simulated`, a record/replay format, a `reset/step/observe/seed` training
wrapper, and an honest `FidelityDeclaration`. A test asserts no twin-authored string claims
certification, "collision-free", "provably safe", or "virtual commissioning".

`core/print/**` is the additive durable model (project, plate placement, object instances by artifact
hash, profile refs by id+hash, slice artifacts, immutable run records, monitoring rows) with
`record_index` explicitly distinct from journal `seq`, and compile-time structural assertions that no
open/upload/start/pause/cancel entry point exists. `services/manufacturing/metrology/**` makes the
evidence boundary structural rather than documentary: advisory (camera) and qualified (probe)
observations are different types with no converting constructor, the offset and conformance APIs accept
only qualified observations, the A0–A2 and Q0–Q3 ladders compute uncertainty from measured inputs, and
a locality guard refuses a Q2 offset used for datum transfer or a Q3 conformance claim.

Gates: `ctest -R 'test_mfg_foundation|test_grbl_protocol|test_device_lane|test_mfg_authorization|test_twin|test_print_model'`
6/6 pass; `pytest services/manufacturing` 41 passed; `pytest services/agent` 3347 passed / 7 skipped;
`scripts/check.py` all 8 gates PASS.

Still deferred: no real serial/TCP/WS transport, no gateway process or RPC dispatch, no MCP tools, no
GUI surface, and nothing advertises physical `execute`.

## 2026-08-08 — Manufacturing overhaul, phase P4 reach + breadth

The manufacturing core is now reachable from the daemon, agents, and the GUI, and the breadth tracks
landed.

Integrator decision recorded here so it is not mistaken for an omission: the design's separate
`chudware-mfgd` process is DEFERRED to P5, when real serial/TCP/WebSocket transports arrive and
process isolation starts to earn its cost. For P4 the lane lives inside `chudware-hostd` as
`core/hostd/mfg_lane.{h,cpp}` — compiled into `chudware_manufacturing` so the daemon and
`test_mfg_lane` share exactly one definition. The safety properties are unchanged: one lane object
owns a device, the arbiter is still the only approval validator, and no transport reaches hardware.

The lane serves the binding `mfg_*` surface (device list/describe/capability/claim/release/connect,
telemetry snapshot, artifact register, preflight, gate report, approval propose/create/inspect/revoke,
run start/hold/abort/list/describe, journal query, result invalidations) and broadcasts `mfg.*` events
with the canonical invalidation envelope. Two additions were approved with conditions and tested:
`mfg_run_resume` exists ONLY to refuse — it has no success path and names the discontinuity plus the
requirement of a new authored run — and `mfg_emulator_fault_inject` refuses unless the endpoint is
non-physical, so it can never reach hardware. Neither is exposed over MCP.

`services/mcp/mfg_tools.py` registers 15 tools through `view(mcp, "manufacture")`: 9 read, 3
write/propose, and exactly 3 motion tools. `tool_registry` gained `RISK_MOTION`/`RISK_SAFETY` with
EXPLICIT membership — no lexical pattern can mint motion risk, an unlisted `mfg` tool stays `write`,
and `_SAFETY_TOOLS` is deliberately empty. `mfg_approval_create` has no MCP tool at all: a model may
propose an approval, never grant one. The family joined `DEFAULT_TOOL_FAMILIES` now that real tools
exist and stays in `DEFAULT_OFF_TOOL_FAMILIES`.

`host/src/ui/manufacture/**` is the operator surface: a persistent Offline Simulation vs Live Machine
banner (entering Live requires explicitly claiming the command lease), a device list rendering all
nine capability levels with truth chips that name `missing_level`, a run panel showing durable run
state with the seq-stamped gate report including every Stage-2 row and named waivers, and an approval
dialog whose `exercisable_by` selector defaults to human-only under the commissioning ceiling. The
mirror enforces `FreshValue` at its boundary, so a non-ok value is cleared and last-good is visibly
demoted rather than repainted as current. `CHUDWARE_SHOT=manufacture` seeds a hardware-free fixture;
the first render clipped the banner to "FLINE SIMULATION" and that was fixed and re-verified.

`core/cam/turn/process.*` adds honest single-spindle XZ turning (diameter/radius semantics, G18,
feed-per-rev, CSS, threading, lathe footer) and `core/cam/cut/process.*` adds separate laser, plasma
and waterjet process profiles with their own process state instead of overloading spindle RPM;
operations advertised without executable semantics now refuse by name. `services/manufacturing/pnp/**`
does OpenPnP job interchange plus a virtual placement run, and `services/manufacturing/robot/**` does
URDF/SRDF import, FK, reachability/singularity, collision and deterministic mock replay — with every
execute/transport/safety method name reserved as a permanent named refusal.

Gates: `ctest` 94/94 — the one parallel-run failure, `mesher_advanced`, is a wall-clock ceiling under
`-j6` CPU contention in FEA/meshing (untouched by this work) and passes standalone in 56 s;
`pytest services/manufacturing services/perception training/perception services/mcp/test_mfg_tools.py`
81 passed / 1 skipped; `./build.sh` links; `scripts/check.py` all 8 gates PASS.

Still deferred: real transports, printer adapters, the split-out daemon, and any physical `execute`.

## 2026-08-08 — Manufacturing overhaul, phase P5 transports, printers, attention, capture

Real I/O landed, still behind a deny-by-default commissioning gate.

`services/manufacturing/gateway/device_allowlist.{h,cpp}` is the gate: a durable, human-authored TSV
naming either a serial device (stable by-id path, or Windows port + VID/PID/serial) or a network
endpoint (transport kind + host + port), each with machine class, connection parameters and an
operator note. Enumeration returns candidates; only an allowlisted entry may be OPENED, a refusal
embeds the exact line the operator would add, and an entry change bumps the lane epoch.
`transport_serial` is a real POSIX+Windows serial `ITransport` — exclusive open via `O_EXCL` + `flock`
+ `TIOCEXCL` (CreateFile on Windows), raw termios/DCB, bounded non-blocking reads, partial-write
completion, and explicit `device_lost` on disconnect. `transport_tcp` and `transport_ws` add FluidNC
telnet and a from-scratch RFC 6455 client: real `Sec-WebSocket-Accept` handshake, masked client
frames, continuation reassembly, UTF-8 validation, ping/pong, close codes, and refusal of malformed
or oversized frames. The SHA-1 it needs is private to that TU, marked not-general-purpose, and pinned
against the RFC 6455 §1.3 vector. Everything is tested with no hardware and no network: a pty pair for
serial, an in-process server on `127.0.0.1:0` for TCP/WS. Connection loss stays terminal — there is no
reconnect that resumes motion.

`services/manufacturing/printers/**` implements Moonraker, OctoPrint and capability-probed PrusaLink
over stdlib HTTP plus a stdlib websocket subscription, with in-process virtual printers as fixtures.
Recorded here as a deliberate deviation from the design's C++ adapter: writing an HTTP+WebSocket client
in C++ with no new dependencies is not worth the cost, so adapters are Python reached through the
existing `hostd/worker_bridge` seam and registered in `services/worker_main.py`. The lane remains the
sole owner of device claims, approval validation, durable seq and events; an adapter's `start` only
checks that an opaque post-arbiter token is present and never validates it.

`services/manufacturing/notify/**` is the attention path: durable items raised from `mfg.*` events,
never auto-dismissed by time, deduplicated into an occurrence count, replayed from the journal across
restart, and delivered in-app plus desktop plus webhook with bounded retry, per-channel rate limiting
and a delivery record — so an undelivered critical item is VISIBLE rather than silently lost. Webhook
payloads carry links, never secrets or media. `host/src/ui/manufacture/attention_panel.{h,cpp}` renders
open items newest-first and only acknowledges; it resolves nothing locally. Its header originally used
`std::span`, which the C++17 host target rejects — replaced with a pointer+count view.

`services/perception/capture/**` wires capture into Forecast-to-Compress: bounded rolling buffer with a
memory ceiling, feature extraction through the existing vision primitives, monotonic-time fusion that
MARKS a gap instead of interpolating across it, event-centric persistence, redaction applied before
write, and deterministic offline replay into training windows. Capture never gates, never proposes an
offset, and a capture failure degrades to a recorded gap rather than a machine action.

Gates: `ctest -R 'mfg|grbl|twin|print_model|device_lane|transport|cam'` 11/11 pass;
`pytest services/manufacturing services/perception training/perception services/mcp/test_mfg_tools.py`
109 passed / 1 skipped; `./build.sh` links; `CHUDWARE_SHOT=manufacture` renders; `scripts/check.py` 8/8.

Outstanding for the next phase, and deliberately not faked: the lane still constructs the emulator
transport directly, so selecting a serial/TCP/WS transport for a claimed device needs an owned
`ITransport` seam in `mfg_lane`; the printer adapter, attention and capture hooks into the lane are
likewise not wired yet. No physical device has been opened, and `execute` remains `assumed`.

## 2026-08-08 — Manufacturing overhaul, P5 integration: the lane now selects a real transport

The parts from the previous entry are wired together, which was the honest gap it recorded.

`core/hostd/mfg_lane` no longer hard-binds the emulator. It owns a
`std::unique_ptr<device::ITransport>` selected at claim time from the allowlist entry's transport
kind — emulator, serial, TCP or WebSocket — and rebinds `GrblAdapter` onto whichever was chosen.
Emulator stays the default and is the ONLY kind reachable with no allowlist entry, so the
hardware-free path can never regress into an accidental real open. The lane owns
`DeviceAllowlist(state_directory / "device_allowlist.tsv")`; `mfg_device_list` marks each enumerated
candidate allowlisted or not WITHOUT opening it, and an observed epoch change bumps the lane epoch,
publishes `mfg.gateway.epoch`, closes the active transport and falsifies the claim.

Printer adapters are reached through an injected `setWorkerCall` seam rather than a link-time
dependency — `chudware_manufacturing` is a static library and `WorkerBridge` only exists in the
daemon, so `Server`'s constructor now installs the callback beside the existing event sink. Connect
routes to `mfg_printer_adapter_open`/`_probe`, telemetry reconciles through `_snapshot`, hold maps to
`_pause`, abort to `_cancel`, and `_start` is called only after `arbiter.start()` succeeded, carrying
an opaque post-arbiter token. Worker-asserted authority is stripped: an adapter that claims
`execute: verified` is still clamped to `assumed` with `missing_level:"execute"`, and with no callback
installed the printer methods refuse by name instead of crashing.

Attention is served by the lane (`mfg_attention_raise/list/ack/resolve/delivery`), journaled and
published as canonical `mfg.attention` actions with authoritative seq — the Python `AttentionService`
never opens the lane journal. The GUI mirror projects those rows in seq order, idempotently on replay,
behind an Attention tab with a persistent unacknowledged-critical indicator; a row is never removed
optimistically on click. Verified in the `manufacture` shot: red indicator, `Attention (1 critical)`,
a 3-occurrence critical item with device/run/raised-seq, a visibly FAILED sms delivery rather than a
silently dropped one, and an Acknowledge control naming the actor.

Gates: `ctest -R 'mfg|grbl|twin|print_model|device_lane|transport|cam'` 11/11; the lane's own suite is
16 cases / 363 assertions; `pytest services/manufacturing services/perception training/perception
services/mcp/test_mfg_tools.py` 109 passed / 1 skipped; `./build.sh` links; `scripts/check.py` 8/8.

Unchanged and deliberate: no physical device has been opened, `execute` is `assumed` everywhere, and
the E4–E6 hardware pilot remains the gate before any of that changes.

## 2026-08-08 — Manufacturing overhaul: E3 end-to-end qualification (and the bug it found)

`scripts/qualify_mfg_e3.py` drives a REAL private `chudware-hostd` over its IPC socket through the
entire manufacturing lifecycle on the emulator lane and prints a per-step table, exiting non-zero on
any deviation. It is opt-in and is NOT called from `scripts/gate.sh`, so it adds no gate SKIP.

It immediately earned its keep by finding a real defect: `mfg_lane`'s `serviceDevice` only called
`adapter_->service()` and `mfg_run_describe` never synchronized, so a started run stayed `running`
forever — it never reached a terminal state and an injected alarm never terminated it. Fixed in the
lane: describe now services and synchronizes, a completed stream journals `completing → completed`,
and alarm / hard-limit / disconnect injection immediately terminalizes the active run as `faulted`
with the discontinuity named, after which `mfg_run_resume` refuses by name and a same-lease reconnect
refuses `mfg_transport_terminal`. Lane suite is now 18 cases / 501 assertions.

A second correction came out of writing the qualification honestly: the original step-9 assertion
("execute is always assumed") was wrong for the emulator, which legitimately earns `execute: verified`
for the EMULATED class at E1–E3. The step now asserts both halves — emulated scope explicit with
`safety_integration: unsupported`, and a genuinely physical endpoint shape (an allowlisted serial
by-id path, never opened) clamped to `execute: assumed` with `missing_level:"execute"`. Faking a
loopback as physical was rejected because it would have made the test lie.

Verified: 9/9 steps PASS in 0.6 s, private daemon stopped and temp state removed, no stray process.
`ctest -R 'mfg|transport'` green. Restaged `services/dist/Chudware/chudware-hostd` so `./build.sh core`
no longer reports a stale consumed engine. Repo maps refreshed: `.claude/skills/chudware-dev/SKILL.md`
gained the manufacturing rows plus the binding rules (family default-OFF, arbiter-only approval,
deny-by-default allowlist, sole `FreshValue<T>`, journal-only seq, no physical `execute` before the
pilot, `mfg_run_resume` refuses only), and `gui-workspaces` gained the Manufacture workspace row.

## 2026-08-08 — Manufacturing: the four CRITICAL review findings closed

`docs/manufacturing/24_code_review.md` said the subsystem was not fit to sit behind hardware. Its
four criticals plus the lease-heartbeat gap are now closed, each made structurally impossible rather
than merely checked.

Physical execute: the old guard rejected only non-printers, so every allowlisted physical printer
walked past a ceiling the same lane advertised as `assumed` — and `computePreflight`/`authorizationFor`
hardcoded `MachineClass::Emulated` with verified execute and passing interlock/feed-hold/metrology
facts, so preflight could not catch it. Every `endpoint_is_physical` start now fails closed with
`mfg_physical_execute_not_qualified`, authorization facts derive from the actually-selected device,
and a virtual qualification endpoint must declare `origin=virtual` in the allowlist instead of the
printer class being exempted wholesale.

Identity: approver and initiator were self-asserted request fields, and `server.cpp` took the user
name from the client's own `hello`, so a lease holder could claim to be any human and exercise a
human-only approval. Both assertions are now rejected outright; provenance derives from the
authenticated hostd peer (Linux `SO_PEERCRED`) bound to the connection and its lease owner.

Fingerprint: approvals bound `advertised_fingerprint_`, seeded from the built-in emulator identity and
never recomputed when `selectTransport` changed the endpoint — so gates compared the advertised value
to itself and a controller swapped at the same address could execute someone else's approval. The
identity observed during connect is now the current fingerprint, and a change invalidates approvals,
resets affected runs and appends falsification records.

Device binding: start verified that ids existed but never that the run's, the artifact's and the
approval's device matched the claimed one, and those records survived release, so an artifact authored
for device A could start on device B. All bindings are enforced at start and device-scoped records are
invalidated whenever the selection changes.

Leases: 15-second TTLs advertised a 3-second heartbeat that had no RPC and never called
`LeaseRegistry::renew`, so hold and abort could be refused while a job was still running.
`mfg_device_heartbeat` renews only for the owning connection, the GUI emits it from the manufacture
request pump, and expiry now closes the device and faults the active run deterministically instead of
stranding it.

Gates: `test_mfg_lane` 27 cases / 840 assertions; `ctest -R 'mfg|grbl|twin|print_model|device_lane|
transport|cam'` 11/11; `qualify_mfg_e3.py` 9/9 in 0.57 s; `qualify_mfg_printer.sh` 8/8 in 0.79 s;
`./build.sh` links; `scripts/check.py` 8/8.

The reviewer's blocking verdict is answered. Physical execute stays refused by name until the E4-E6
pilot actually happens — that is now a deliberate gate rather than an unimplemented one.

## 2026-08-08 — New workstream: the learned viewpoint policy

Direction correction from the user, and my own reporting was the cause: I had been calling the
telemetry forecaster "the perception work" while he meant a perceptron — a real network — and the idea
he actually wants is sharper than what I was building. In his words: deterministic perspective view
shots are terrible and never work, so a network should decide the angle and offset such that the
geometry that matters can actually be inspected — in CAD and in the real world. He also proposed the
data flywheel that makes it trainable: the harness offers several candidate renders, the consuming
model scores which one let it answer, and those scores become preference labels.

That splits cleanly into a two-tier curriculum, which is the architectural claim: geometry is
learnable from physics, usefulness is not. Stage 1 labels come free and exact from the CAD kernel;
Stage 2 labels can only come from a consumer of the image.

`services/viewpoint/**` is Stage 1: validated mesh/feature/pose primitives, and a transparent
inspectability label with seven named components (in-frame coverage, sampled ray-triangle occlusion,
foreshortening, apparent pixel scale, silhouette exposure, clipping/depth validity, projected
ambiguity) rather than one opaque score, plus seeded samplers and a versioned dataset with geometry
hash and full provenance. `services/viewpoint/feedback/**` is the Stage-2 protocol: pairwise
preference is the authoritative label with rubric scores as bounded diagnostics, candidate order is
randomized per request with the permutation recorded, abstention cannot produce a label, an exact
binomial test catches a position-biased scorer, and low inter-rater agreement excludes a record
instead of quietly poisoning training. `training/viewpoint/**` is the policy and Stage-1 training.

First honest numbers, and they say the benchmark is not yet good enough. Against the fixed isometric
view the policy scores 0.9287 vs 0.4253 with an 85.7 % strict win rate, which confirms the thesis with
a number. Against best-of-8 random poses it scores 0.9287 vs 0.9163 with a strict win rate of only
28.6 %, four of seven oracle components saturate at 1.0, and the locked test set is 14 examples of
procedural cuboids. Brute-force search is therefore nearly optimal, which means the corpus is too easy
to demonstrate anything. Two follow-ups are in flight: a genuinely occluding part generator with
non-saturating components, and a re-framed headline metric — policy@1 against best-of-N random, whose
crossover N states directly how many renders the expensive downstream model is saved.

## 2026-08-08 — Viewpoint policy: the hard benchmark, and the policy fails it

The easy benchmark was replaced and the honest answer changed sign. Recorded here because the failure
is more informative than the earlier apparent win.

`services/viewpoint/parts.py` generates mechanical parts with genuine self-occlusion — bosses
shadowing holes, axial counterbores and deep pockets, an oblique hole on a tilted annular face, ribs
and webs in concave corners, asymmetric through-hole ends, an underside-overhang hole, and
context-dependent ambiguous twins — with each target labelled easy/medium/hard. Benchmark poses now
vary framing, distance, FOV and clipping, so no oracle component had to be excluded to stop it
saturating: coverage fell 1.000 → 0.938, scale 1.000 → 0.769 (100 % → 43 % exactly-1), uniqueness
1.000 → 0.823, visibility 0.496 → 0.289, silhouette 0.507 → 0.204. Locked fixture: 24 parts, 288
labelled features (72 easy / 96 medium / 120 hard).

The difficulty is real. Best-of-N random on the held-out set now reads N=1 0.165, N=2 0.217, N=4
0.349, N=8 0.463, N=16 0.603, N=32 0.678 — against 0.916 for best-of-8 on the old corpus. Brute-force
search is no longer nearly optimal.

And the policy does not beat it. On 66 locked examples from 11 held-out parts (50/11/11 part split,
zero overlap, 5,000 whole-part bootstrap samples): policy@1 = 0.265 against random@1 0.165 (delta
+0.100, CI [-0.003, +0.213]), random@4 0.349 (-0.084), random@8 0.463 (-0.199, CI excludes zero),
random@32 0.678 (-0.413, strict win rate 0 %). It beats fixed isometric (0.176) and the greedy
geometric heuristic (0.191), but the point crossover of N=4 has a bootstrap CI of [1, 8] — an interval
that includes one render, so **there is no demonstrated compute saving**. Sliced by difficulty it wins
on easy (0.566 vs 0.293), loses outright on medium (0.097 vs 0.156), and is only modest on hard (0.222
vs 0.086). Best validation aggregate during training was 0.325, so it is underfitting, not merely
generalizing badly.

Verdict: on a benchmark hard enough to be worth solving, the current policy does not earn its
existence over brute-force search. The `docs/manufacturing/25_viewpoint_model.md` verdict says so
plainly.

Diagnosis for the next iteration, which is architectural rather than a tuning matter. Direct pose
regression is the prime suspect: several distinct viewpoints are equally good for one feature, and a
network trained to regress a single pose under an L2-style objective converges toward the mean of a
multimodal target, which for a sphere of good poses is a bad pose. The distribution table supports
this — against random@1 the higher mean comes from infrequent large wins plus a majority of exact-zero
ties, not per-example dominance. The planned redesign scores candidate poses instead of imagining one,
which also matches the deployment shape (the harness renders N, the policy ranks them) and the shape
of the Stage-2 preference data already built.

## 2026-08-08 — Exact perspective camera for doc_render_view

The viewpoint policy reasons in perspective — position, target, up, vertical FOV, near/far — because
real cameras do, but the engine could only render direction-only ORTHOGRAPHIC views. A pose the policy
chose therefore could not be turned into the image it was scored on, and nothing would transfer to a
webcam. `services/viewpoint/render/**` was written against the contract and refused by name until this
landed.

`core/doc/session.cpp` now accepts `view.camera = {position, target, up, vertical_fov_deg, near, far,
width, height}` as AUTHORITATIVE: applied exactly, with no auto-fit, no bbox re-framing and no
orthographic substitution. Only the image size may clamp, and it reports when it did. The perspective
path shares the shading/triangle/depth/blend code with the orthographic one rather than becoming a
second renderer, clips against all six frustum planes, and uses reciprocal depth. Degenerate input —
up parallel to the view direction, zero-length direction, non-positive FOV, `near >= far`, subnormal
FOV overflow — is a named refusal, never a silent fallback view. `ping` advertises
`doc_render_view.camera_pose.v1`; the response echoes the exact applied camera and the locked document
revision so a client can prove the pixels correspond to the pose it asked for, plus a reprojectable
perspective `frame` block documented alongside the unchanged orthographic form in append-only
`contracts/worker_rpc.md` §17.

Back-compatibility was the real risk here: `doc_render_view` feeds icon generation, agent part
inspection and GUI previews, so a silent framing change would have corrupted all three. All 29 PNGs
from the existing representative render-view suite were regenerated before and after and their SHA-256
manifests diff clean — every image byte-identical. `test_render_view` still passes 10 cases / 151
assertions.

Gates: new `test_render_camera` 7 cases / 160 assertions (hand-projected cube silhouette, the
apparent-scale ratio orthographic cannot produce, frustum clipping, every named degeneracy, capability
and revision echo, clamp reporting, pinned legacy hashes); `ctest` 96/96 — the two failures in the
parallel run, `kicad_import` and `mesher_advanced`, both pass standalone and are the same `-j` CPU/temp
contention artifact recorded earlier, in code this work never touched.

Note for future runs on this box: `/tmp` is a 16 GB tmpfs sitting at 81 % from other sessions' build
scratch, and GCC assembling large TUs there fails with "Disk quota exceeded". Build and test with
`TMPDIR=$PWD/core/build-linux/tmp`.

## 2026-08-08 — Viewpoint observation: the sequential null was partly a defect

Chasing the sequential null result found a real bug rather than a property of the problem. The
observation package's public `visibility_estimate` described only the CURRENT camera even though the
observer had accumulated a history — so a policy given four looks was, for that scalar, still being
handed one. It now returns the running best across looks. A second defect went the other way: prior
accumulated points were being reprojected and OR-ed into visibility, which lets stale geometry
manufacture line-of-sight evidence through an occluder; that was removed, and accumulated points are
now finite-only voxel-union geometry that cannot claim current visibility.

An earlier blocker was also fixed on the way: the level readouts all returned identical numbers
because the proxy compared raycast RANGES against forward-axis projection depth. Per-level
line-of-sight occlusion now runs from each level's own data, and `full` correlates 1.000 with the
frozen oracle — the sanity check that proves the estimator, which the old proxy failed at 0.153.

With that fixed, accumulation genuinely does add information. Depth-level correlation with true
visibility under oracle-chosen look ordering: K=1 0.324, K=2 0.549, K=4 0.647 — then DOWN to 0.558 at
K=8, which is non-monotonic and reported as such rather than smoothed. Random ordering is weaker
throughout (0.340 / 0.481 / 0.438 / 0.415). Four fusion-defect hypotheses were tested and eliminated
with numbers: looks are not resampling the same rays (mean consecutive angle 90.0° random / 77.7°
oracle), fusion is not overwriting (finite voxels grow 67 → 524), NaN misses are not poisoning
anything (all 512 checkpoint observations finite), and neighbourhood support does grow (0.420 → 0.996).

The useful number this produces is the gap decomposition. At the best depth ceiling (oracle K=4,
r=0.647) observation quality accounts for 0.182 of the 0.337 policy-versus-oracle gap — **54 %** —
leaving 46 % as policy headroom. Under random K=8 ordering it accounts for 89 %. It is a labelled
diagnostic estimate, not a causal bound, and is documented that way.

So the earlier sequential null had two causes stacked: an 8-candidate pool that let random search
exhaust the space, and a history-blind scalar. Both are fixed; the corrected 256-pool sequential run
is in flight.

Gates: `pytest services/viewpoint` 50 passed, frozen oracle/geometry/sampling/parts/dataset untouched.

## 2026-08-08 — Viewpoint oracle gets a GPU backend (and the GPU was never being used)

Root cause of the CPU load first: the installed Torch was `2.12.1+cpu`, a CPU-only build, so
`torch.cuda.is_available()` was False and every training run in this effort — including a 63-minute
sweep — ran on the CPU while the RTX 3080 Ti sat idle. Replaced with `2.12.1+cu129`; verified
`cuda True`, 20x 4096^3 matmul in 0.18 s. Two environment traps worth recording: `/tmp` is a 16 GB
tmpfs near its quota, which broke both a GCC assembly step and the pip install ("Disk quota
exceeded") until `TMPDIR` was pointed at `~/.local/tmp` and `core/build-linux/tmp`; and this box has
6 cores / 12 threads, not 6 threads, which had led to an over-tight worker cap.

CUDA alone was not the answer, though, because Torch only accelerated the model fit — the cheap part.
A profile of the real cost settled it: ray-triangle intersection nested inside occlusion and ambiguity
dominated (`_ray_hits` 265,400 calls / 71 %, `_ambiguity` 77.7 %, `_occlusion_fraction` 72.3 % of a
59.8 s workload), while part generation was 0.6 %. So `services/viewpoint/accel/**` batches that
predicate across candidates and scene features on Torch, dispatching CUDA → Torch-CPU → the frozen
NumPy path via `evaluate_many(scene, requests, device="auto")`.

Parity was the acceptance bar, not speed, because the oracle IS the benchmark. Across 144 comparisons
including grazing rays, zero-area projections, fully occluded features and NaN misses, the maximum
absolute error on all seven components is **exactly 0.0**, with zero ambiguous-ID mismatches, against a
stated 5e-12 tolerance. The frozen modules were not touched and no hook into `oracle.py` was needed.

Throughput: 21.4 labels/s NumPy-CPU → 65.7 Torch-CPU → **187.1 CUDA**, an 8.7x end-to-end gain; a
5000-part corpus projects to 233 s instead of 1888 s. Honest remainder, and it is not small: part
generation, camera sampling, feature extraction, projection, clipping, hull construction,
visual-signature comparison, label assembly and serialization are all still CPU-bound. The dominant
predicate moved; the pipeline around it did not.

Gates: `pytest services/viewpoint` 55 passed; 51 passed / 4 skipped with Torch unimportable, so the
optional-dependency contract holds. Observed 22 % GPU utilisation and 5.6 GB VRAM during the suite,
which is the load leaving the CPU.

## 2026-08-08 — Viewpoint sequential: corrected benchmark, and the two-bar verdict

The corrected sequential run finished on CUDA — 66 examples, 11 locked parts, a 256-pose pool decoupled
from K, K in {1,2,4,8}, 2,000 whole-part bootstraps, 401 s — and the earlier null is explained rather
than repeated. Two stacked causes: the old 8-candidate pool (at K=4 random already sampled half of it;
at K=8 every method exhausted it and trivially tied the oracle), and the history-blind
`visibility_estimate` fixed in the previous entry. Both are documented in place so the artifact is
visible instead of buried. Budget equality between policy and baseline was re-verified, and four fusion
defect hypotheses were eliminated with measurements rather than argument.

The verdict is now stated against two bars, neither allowed to bury the other.

**(a) Product-relevant — same-information alternatives.** Best one-look depth policy .300661 against
blind random .122591 (2.45x), fixed isometric .171608 (1.75x) and the hand-engineered greedy geometric
heuristic .169855 (1.77x); whole-part deltas +.178070 [.102354, .254252], +.129053 [.020345, .236038],
+.130806 [.077625, .189148]. On the corrected 256-pose sequential benchmark every observation level
beats matched random at K=2 with intervals excluding zero. The policy learns real deployable signal,
and the earlier "does not earn its existence" line was measured against the wrong comparison.

**(b) Student-versus-teacher — full-information oracle ceiling.** The same policy reaches only 47.1 %
of .638062, regret .337401 [.280749, .396614]; validation regret is flat at .346/.344/.351 across
200/1k/5k parts; every corrected sequential oracle-regret interval stays wholly positive; and
sequential value is non-monotonic (best-next depth peaks .647 at K=4 then falls to .558 at K=8).

So it earns continued product evaluation and does NOT earn product dependency or deployment. The
binding constraint is named and measured: observation/perception information, not the policy or the
history plumbing. Depth-level visibility correlates .584 with truth at one look and only .647 at the
K=4 peak; depth-plus-prior is identical to depth; sparse points are effectively uninformative. More
procedural parts cannot move it — that is what the flat scaling curve means. What can move it is a
better observation representation: learned image/depth features, calibrated visibility uncertainty, or
denser reliable returns, validated without oracle leakage.

Gates: `pytest training/viewpoint services/viewpoint` 77 passed; 17 passed / 3 skipped with Torch
unimportable.

## 2026-08-08 — Viewpoint pipeline: corpus cache, CUDA defaults, and a determinism ruling

The oracle work now runs on the GPU by default and the same corpus is no longer regenerated on every
run. `training/viewpoint/corpus_cache.py` is a SHA-256 content-addressed NPZ cache under
`scratch/viewpoint-cache`, keyed on the complete generator dataclass plus exact part indices, seed,
candidate count, sampler version, mechanical-generator version, oracle version and both schema
versions — so any change to any of those misses, with atomic publication, corrupt-entry rebuild and
scratch-confined invalidation. `train`, `scale`, `information` and `sequential` default oracle
generation to CUDA when available, print the device they ACTUALLY used, and record cache hit/miss plus
the key, so a silent CPU fallback cannot masquerade as a GPU run.

A determinism problem surfaced and was ruled on rather than papered over: a CUDA fit changed published
numbers (validation aggregate .468744 → .459555, test policy .403032 → .402567) even though oracle
labels are bit-identical CUDA-versus-NumPy. The cause is the CUDA AdamW/matmul trajectory, not the
geometry. Since the fit is the CHEAP part and the oracle is where all the cost is, the reference
configuration keeps the fit on Torch-CPU while CUDA drives corpus generation and sequential
evaluation; every report prints `DEVICE: cuda` alongside `fit_backend: torch-cpu`. `--fit-device cuda`
remains available for exploration and is explicitly labelled as not comparable to the CPU reference
numbers. Cross-device bit-identity is not promised because it does not exist; the contract is
reproducibility for a given (fit_backend, seed) pair. The .009 swing from backend nondeterminism alone
is recorded in the doc as useful context for how much weight any single run's third decimal deserves.

Controlled timings, reduced 50-part ladder, seed 1729, 120 epochs, niced, six BLAS threads —
deliberately measured rather than quoting an earlier uncontrolled 63-minute task wall clock:

| command | cold CPU | cold CUDA | warm cache |
|---|---:|---:|---:|
| `scale` | 89.6 s | 111.3 s | **52.6 s** |
| `information` | 112.4 s | 73.4 s | **23.5 s** |

Honest reading: at this small ladder CUDA LOSES on `scale` (111 s vs 90 s) because launch and
marshalling overhead dominates a tiny workload; it wins on `information`, and the cache wins decisively
on both. The 8.7x oracle throughput figure holds at corpus scale, not at 50 parts. Result parity was
verified in every direction: 0 numeric differences CPU vs cold CUDA, and 0 cold CUDA vs warm cache,
with the only JSON deltas being weights paths and device provenance.

Gates: `pytest training/viewpoint` 27 passed. Still CPU-bound and named as such: part generation,
camera sampling, feature extraction, projection, clipping, hull construction, signature comparison and
serialization.

## 2026-08-08 — Viewpoint pixels: learned features do not beat hand-computed geometry (yet), and why

`services/viewpoint/pixels/**` renders the candidate corpus through the exact perspective camera:
432 rows / 6,912 images at 96x96, splits disjoint by part (50/11/11), the locked test parts present,
310 MiB content-addressed and resumable, labels generated through the CUDA oracle. Two things make it
trustworthy: the in-process rasteriser was checked against the ENGINE renderer and agrees exactly —
foreground IoU 1.0, RGB MAE 0.0 over 3,608 pixels — and a leakage test asserts the inference sample
carries only RGB, normalized depth + valid mask, 16-D camera descriptors, the 8-D uncertainty-bearing
`FeatureRegion2D` prior, task/query one-hots and the candidate mask. No mesh, no exact feature
position/axis/normal/extent, no oracle components, no occlusion data, no target lookup key.

`training/viewpoint/pixel_model.py` is a from-scratch compact residual CNN (32/64/96 channels,
GroupNorm, SiLU, two 3x3 residual blocks per stage) over RGB-D per candidate, feeding the EXISTING
task-conditioned ranking head with the same objective, so observation representation is the only
variable. No third-party encoder or pretrained weights; PyTorch is runtime-only under BSD-3-Clause.
Trained GPU-resident on CUDA.

The verdict it printed, verbatim: "does not beat hand-computed depth observation; remains below
full-information oracle ceiling; does not beat .584 depth visibility correlation."

But the honest reading is that this run did not actually test the hypothesis. The curve shows training
aggregate climbing .213 → ~.40 while validation stays pinned around .15-.22 — textbook overfitting on
a 50-row / 800-render training slice, with the whole fit taking 10 seconds. What was measured is "can
a conv trunk generalize from a few hundred ranking examples", and the answer is no. Whether learned
pixels beat hand-computed geometry as a REPRESENTATION is still unanswered, because data volume is
confounded with representation.

A contract question was ruled on during the work: the corpus slice had stripped the task vector,
reading "nothing else" as excluding it. Restored — the no-leakage rule governs what the SENSOR reveals,
not what the user asks, and a deployed system knows its own query. Removing it would have changed two
variables at once. The boundary is now explicit: the task may name WHICH feature and its class, never
its position, axis, extent or normal; that stays with the coarse uncertainty-bearing prior.

Next step is therefore a materially larger pixel corpus rather than another architecture change, since
corpus generation is now GPU-labelled, cached and resumable. If a conv encoder given enough data still
fails to beat .584 visibility correlation, then learned pixels are not the answer either and the
diagnosis moves on.

## 2026-08-08 — Viewpoint pixel corpus scaled 10x, generation 7x faster

The pixel experiment's confound was corpus size, so the corpus grew and generation got cheap enough to
make that practical. A profile named the cost honestly: rasterisation was 84.8 % of it, oracle
labelling 12.6 %, everything else under 1 % each. Rasterisation now has a lazy-Torch float64 CUDA path
batching all candidates per row, with the exact scalar NumPy path preserved as the fallback.

Throughput 0.439 → **3.110 rows/s**, a 7.08x gain. The enlarged corpus is 522 parts / 3,132 rows /
50,112 images at 96x96, 1.85 GiB, generated in 1,007 s across one timeout and one resume that reused
1,951 already-published rows — the atomic row-level resumability earned its keep. Training grew
300 → **3,000 rows** and 4,800 → 48,000 renders; validation and test stayed at 11 parts / 66 rows each.

The identity checks are what make the enlarged corpus comparable rather than a new experiment. All 132
locked validation/test rows match the previous manifest on split, part id, input object AND target
object — byte-identical, not merely equivalent. The CUDA rasteriser was probed against the scalar path
over 12 parts / 192 views: 192 RGB-exact, minimum foreground IoU 1.0, maximum RGB MAE 0.0, maximum
depth absolute error 0.0. Engine-renderer agreement was re-verified after the rewrite at 160x120:
foreground IoU 1.0, RGB MAE 0.0 over 6,648 pixels. Frozen-oracle component parity remains 0.0 max
absolute error, and the baseline candidate indices are unchanged.

Corpus lives in gitignored `scratch/viewpoint-pixels-large/` and is regenerable from the recorded
generator/renderer/oracle versions, so none of the 2.2 GB is committed.

Gates: `pytest services/viewpoint` green, and green with Torch unimportable. The retrain on the full
3,000-row training set is the actual test of whether learned pixels beat .584 visibility correlation
and the .300661 hand-computed depth baseline; that result is still pending and is not claimed here.

## 2026-08-08 — OCCT's parallel mesher and CUDA cannot share a process (real segfault, worth remembering)

My verification run of the viewpoint suite segfaulted reproducibly at
`services/viewpoint/pixels/test_pixels.py::test_fast_rasterizer_matches_real_engine_pixels` — three
runs in a row, unaffected by `-p no:cacheprovider`, in a slice that had been reported green. The
coredump backtrace (PID 980403 plus four siblings) lands in OCCT's parallel mesher allocator:
`NCollection_IncAllocator::IBlock` → `AllocateOptimal` → `BRepMesh_VertexTool::Add` →
`BRepMesh_BaseMeshAlgo::Perform` → `BRepMesh_FaceDiscret::process` under `OSD_Parallel`/TBB, in a
process that simultaneously held live `libcuda` threads.

So this is native allocator corruption when OCCT's TBB-parallel tessellation runs inside a process that
has also initialised CUDA — not Python logic, not the manifest, and not specific to this test. Anything
in this repo that tessellates through `libchudware` in the same process as CUDA Torch is exposed to it,
which is new territory now that CUDA is actually in use.

The fix keeps the parity check mandatory rather than skipping the inconvenient test: `compare_with_engine`
now execs a clean `agreement_worker` subprocess, and a nonzero exit or fatal signal surfaces as a normal
`RuntimeError` carrying the child's stderr. Engine-renderer parity is the single property that makes the
pixel corpus trustworthy, so deleting or skipping it was not an option.

Verified independently from a cold shell, three consecutive runs: `pytest services/viewpoint` 61 passed
(17.45 s / 17.32 s / 17.49 s), and 57 passed / 4 skipped with Torch unimportable. The isolated engine
agreement test passes in 1.03 s.

## 2026-08-08 — Learned pixels do NOT beat hand-computed geometry. Verdict, with the confound removed

The pixel hypothesis has a real answer now. Training on the full enlarged corpus — 3,000 rows / 48,000
renders, CUDA, seed 1729, five-stage cumulative ablation, epoch-level atomic resume because the harness
kills a process at 600 s — first confirmed the earlier run really was data-starved: validation moved
from roughly .22 on the 50-row slice to **.294317** (best at epoch 17). The confound was genuine and it
is gone.

With it gone, the answer is negative on both counts.

On the end metric, the pixel policy scores **.304987 [.242461, .367679]** against the hand-computed
depth observation's **.300661**, a paired delta of **+.004326 [-.036438, +.048991]** — the interval
crosses zero, so the two representations are statistically indistinguishable. On the representational
claim this path existed to beat, it loses decisively: the learned representation's implied visibility
correlates **r = .308** with the frozen oracle's true visibility, against hand-computed depth's
**.584**. Every regularization intervention hurt: augmentation −.003113, weight decay −.004112,
dropout −.012425, early stopping no change (15 vs 24 epochs). The model is representation-limited, not
data-limited.

So a from-scratch conv trunk over 96x96 renders does not learn occlusion reasoning as well as an
explicit raycast does. In hindsight that is not surprising — explicit geometry is a strong inductive
bias, and 500 procedural parts is thin for learning 3D reasoning from pixels — but it had to be
measured rather than assumed, and the earlier 50-row run could not have told us.

Consequence for the project, decided rather than deferred: hand-computed geometric observation stays
the production representation, and the pixel path is closed as a negative result rather than iterated
with a pretrained encoder and higher resolution. The remaining unexplored lever is the one no oracle
can supply and no amount of geometry can substitute for — whether a view is USEFUL, measured by the
model consuming it. Geometric inspectability and answer usefulness are not the same quantity, and
nothing in this effort has yet tested whether the first predicts the second. That is Stage 2, it is the
user's own flywheel idea, and it is the actual contribution.

Gates: `pytest training/viewpoint services/viewpoint` 30 passed in the training slice, full suite green;
20 passed / 8 skipped with Torch unimportable.

## 2026-08-08 — Full gate, green

Final verification of the whole effort, run from a clean shell with `TMPDIR` inside the repo (the
system `/tmp` tmpfs is near quota and breaks large GCC assembly steps):

- `ctest --test-dir core/build-linux -j4` — **97/97 passed**, 448 s. `mesher_advanced` and
  `kicad_import`, which failed earlier runs purely on `-j` CPU contention, both pass here.
- `pytest services/manufacturing services/viewpoint services/perception training/perception
  training/viewpoint services/mcp/test_mfg_tools.py services/agent/test_manufacture_family_default_off.py`
  — **331 passed, 1 skipped** (the skip is optional `onnx`).
- `scripts/check.py` — **all 8 gates PASS**, including `advertised_tool_audit`: every advertised tool
  is reachable.
- `./build.sh` — links.
- `scripts/qualify_mfg_e3.py` — **9/9** in 0.61 s; `scripts/qualify_mfg_printer.sh` — **8/8** in 0.89 s.
  Both tear down their private daemon and temp state.
- Headless GUI smokes: `CHUDWARE_SHOT=manufacture`, `m1` and `cam` all render.

One item remains open and it is not mine to close: the E4-E6 hardware pilot. Physical `execute` is
refused by name (`mfg_physical_execute_not_qualified`), the device allowlist denies by default, and
`mfg_run_resume` has no success path — so nothing can reach a machine until an operator deliberately
commissions one.

## 2026-08-14 — the version graph, and an OCCT crash that took the daemon with it

Two efforts. **(1) A real version graph.** The vault has always been a git DAG that
`gitbind.log()` read as a line (`parents[0]` only), so every commit reachable only through
a merge's second parent was invisible in `plm_history`, `plm_overview` and `plm_compare` —
merged-in work looked merged away. Added `gitbind.log_dag()` (Kahn walk over child→parent
edges from every branch tip, HEAD and version ref; newest-first AND topologically
consistent, so a renderer assigns lanes in one forward pass), a `refs/aicad/versions/<name>`
namespace with `services/plm/versions.py` for named versions (the Onshape V1/V2 noun), a
real `merge` entry kind (merges were stamped `checkpoint` and thus indistinguishable),
compare-and-swap on merge commits, branch ahead/behind topology, and `Vault.version_graph()`
decorating nodes with `parents`/`refs`/`release`/`pin`. Three additive RPCs
(`plm_version_graph`, `plm_version_tag`, `plm_version_list`) and `contracts/plm.md`
Appendix C record the surface, since §1 said "linear in v1" and §7 said "do not implement
plm_branch/plm_merge" while `branches.py` and the overview panel had already shipped both.
`plm_history` deliberately keeps its first-parent walk — Appendix B.6's per-item semantics
depend on it. GUI: a new `host/src/ui/plm/versions_tab.cpp` Versions page replacing History
on the rail (`plmDrawHistoryPage` stays for the Files page), deterministic git-graph lanes on
the force-graph's ImDrawList/camera/label-culling patterns, per-kind glyphs, branch and V*n*
chips, and an inspector with tag/restore/checkpoint/branch/merge, "Show changes" and
agent micro-history. Two things beat Onshape: any-two-commit compare (ctrl-click), and AI-VC
agent checkpoints with provenance.

Verifying against a REAL vault rather than the code found seven defects the compiling page
hid: the two-commit release pair drew twice (the pin is now folded into its release,
inheriting its decorations and re-parenting its children); "Show changes" reported "that item
is not present at one of those points" on a good commit because `plm_compare` is per-ITEM
(now unioned over the project's items, not-present treated as untouched); "AI activity" was
permanently empty because it sent `plm_timeline` a `commit` it does not accept and read keys
it never returns; a tip chipped "main main"; the legend advertised a Release-candidate state
this tree can never produce; left-drag panned AND selected; and 400 nodes drew every frame.
Two were NOT in the new code at all: `summarize.py` emitted a Unicode arrow the UI font
renders as `?`, and the rebrand from `aicad-plm-*` to `chudware-plm-*` renamed the trailer
READERS, so on any pre-rebrand vault kind/scope silently fell back to defaults (a release
read as `auto`, a planning commit polluted the design timeline) and every message leaked its
raw trailers — 6 of 6 messages on the dogfood vault, and 1 of 6 mis-scoped. Readers now take
both spellings. Also: the first-run onboarding wizard opened over every `CHUDWARE_SHOT`
capture, so headless shots were verifying the sign-in step instead of the surface under test.

**(2) T-BL61 — a fillet SIGSEGV killed hostd.** OCCT's corner resolver does not throw; it
dereferences into `Geom2dAdaptor_Curve::D0` and segfaults, so `blend_drop_failing`'s
`catch (Standard_Failure&)` was structurally unable to contain it. Blend builds now run in a
bounded forked child (`core/util/process_isolation.*`) that serializes its result with
`BinTools`; a signal, non-zero exit, unreadable payload or timeout becomes the same
"unbuildable edge set" the bisect already handled, so `n_dropped` and "round what you can"
are unchanged. A timeout is a failure, not a retry, because `fork()` in a multithreaded
daemon can deadlock the child on a malloc lock. Both the explicit-edge and the whole-body
overloads are isolated. Cost: +0.6 ms on a successful 12-edge fillet (3.88 → 4.47 ms median),
+0.4 ms on the 113 ms partial-drop path; one child per build attempt, never per edge.
**Deferred, honestly:** a 94-case convergent-blend sweep (`scratch/fillet_fuzz.cpp`) produced
ZERO signals, so the card's first DoD box stays unchecked and T-BL61 is `review`, not `done` —
containment is proved by fault injection (`CHUDWARE_TEST_BLEND_SIGSEGV`), which the card
permits. Closing it needs the real captured document, i.e. agent bracket builds re-run with
the run journal on. Also noticed and NOT touched: this repo's own tracked `.aicadvault/`
(2463 files, committed at e4a1a607) accumulates local object churn from any run that treats
the repo root as a project — that is a repo-hygiene decision for Hunter, not a code fix.

## 2026-08-15 — a pixel audit of the workspaces nobody had looked at

Drove all 25 `CHUDWARE_SHOT` scenarios headless and read the captures instead of trusting exit
codes. The exit codes were lying: **`ecad`, `parts` and `aiws` all exited 0 while photographing
the Model workspace.** Startup doc restore calls `activateTab`, which pulls any workspace that
cannot host a 3D part back to Model, and that race had been hand-patched three separate times
(parts, then reveng, then PLM) without anyone generalising it. One per-frame pin
(`AppState::shotWorkspacePin`) replaces the special cases; it ADOPTS any non-Model workspace the
run selects and only restores it when something yanks back to Model, because `fea`/`flow`/
`simdisc_*` reach their workspace mid-run — a blunter pin broke them the other way and a
subagent correctly refused to verify against the wrong surface. On top of that the `ecad` and
`drawing` scenarios only ever set their ribbon tab, never their workspace.

With honest captures, the surfaces confessed. **ECAD** had never been seen with a board on it:
its scenario drove `ecad_design`, a provider-backed AI tool, as its first call, got an error
envelope and aborted, leaving an empty grid crossed by what looked like stray geometry. The
default scenario now mints a provider-free `.ecad+` fixture and applies deterministic ops (48x32
outline, three footprints, GND/SIG, three routes over both layers, a via, one deliberate 0.1 mm
width breach), which exposed two more defects: **net row selection was a dead control** (set
`mgrSel`, changed no pixels — now highlights the net's copper and dims the rest, 20,294 pixels
different) and the "stray line" was the **y=0 grid axis** drawn solid and in front, landing
exactly where an Edge.Cuts line would be. Also worth recording: the first fixture's 6 DRC errors
and 3 warnings were HONEST (routes terminating on opposite-net pads); the fixture was fixed, not
the rules. And the native ECAD op vocabulary cannot create a net — an agent-facing API gap.

**Drawing** had an unreadable title block: row heights scale with sheet zoom while text does
not, and the guard was a `std::min` of the two — exactly backwards, so at 149 % zoom every label
collapsed onto its value. Now a `max` that grows the block upward, verified at 149 % and 211 %.
View labels also fell under the title block and under their own dimensions (dims paint later
into the same band), and the sheet hint read as two fragments because it ellipsised to the full
content width while the floating doc-tab strip covered the middle.

**Simulation Manager** was clipping its own text through three `PushTextWrapPos` calls handed
SCREEN-space X (the call takes window-local), and its body sliced a row in half under the pinned
Run button. **Seven icons** rendered as magenta placeholders in every scenario, and the icons
lint could not see them because its regex missed namespace-qualified `ui::getIcon` — fixed and
proved by reintroducing one bad name. **Mass Properties** mixed unit systems (IPS volume and
mass beside a hard-coded g/cm3 density) and printed `X: 4.449e-17in` for a symmetric body. The
**orientation triad** drew on top of the Simulation Manager during its dodge ease. The **RevEng**
busy mark cleared the tesseract's legibility floor by inflating the radius it passed, rendering
as a broken glyph box.

Two crashes, both latent, both mine to own in different senses. `nlohmann::json::value()` throws
on an existing-but-null key and evaluates its default eagerly, so `value("when", value("time",
""))` threw on plm_timeline's null fields and abandoned the checkpoint list mid-parse in three
separate panels — the copilot rollback popover had been silently truncating history. And the
T-BL61 fork isolation **hung the GUI**: fork gives the child a snapshot of every lock, and the
desktop app links libchudware beside its worker and RPC threads, so children wedged and each
blend burned its 30 s budget (`CHUDWARE_SHOT=fea`: 90 s scenario → hard hang). A timeout now
means "fork is unusable in this process", not "this geometry is bad": retry in-process, disable
isolation for the process, keep signal containment intact. Budget 30 s → 5 s. A new test runs an
isolated fillet against four allocator-hammering threads (7.4 ms, no drop) so the daemon's
containment is measured rather than assumed, and fails the build if it drifts toward the budget.

Verification: all 25 scenarios exit 0 with zero `unknown icon` lines at 1280x800;
`scripts/gui_lint.py` clean; `python3 scripts/check.py` **all 9 gates PASS** — it had been red
before this session on a committed `.kimi-code/skills` symlink that a Windows checkout turns
into a text file, now untracked.

All three items first listed as deferred were then fixed in the same session. The "New study"
picker opened on top of the panel it was launched from (ImGui parks a popup at the cursor) and
now flies out beside the manager column — which also proved the earlier "clipped prose" reading
of that panel was occlusion, not clipping. ECAD zoom-to-fit measured the FULL canvas region
while the pinned Copilot column floats over its right side, so the board's right Edge.Cuts edge
sat under the column; fit now measures the visible strip and offsets the pan to centre the board
in what you can actually see. And the agent DAG's "drag to pan" hint was painted onto the canvas
with no backing, overprinting whatever node card sat in the corner; it gets a washed plate.
Re-verified after all three: 25/25 scenarios exit 0 with zero icon misses, gui_lint clean,
check.py 9/9, test_ops 3702 assertions green.

## 2026-08-15 — Polyhedral-FEA second mesher backend

Polyhedral-FEA (`polymesh`, BSD-3) landed as a second FEA mesher behind the existing
`chudware::sim::MeshOut` contract, with shared sizing in `sim/mesh_size_band.h`, shared quality
accounting through `detail::fill_quality`, and a clean `algorithm3d` → `mesher_backend`
cutover. Touched: `core/cmake/ProvisionPolymesh.cmake`,
`core/sim/polymesh_backend.{h,cpp}`, `core/sim/mesh_size_band.h`,
`core/sim/mesher.{h,cpp}`, `core/sim/fea_run.cpp`, `core/sim/fea_fatigue.cpp`,
`core/api/libchudware.cpp`, `core/tests/test_polymesh_backend.cpp`, and
`core/CMakeLists.txt`. Gotchas for the next contributor: the dependency is pinned by SHA in
`ProvisionPolymesh.cmake` and is `add_subdirectory`-only with no install/export; its concrete
libraries are `polymesh_*` with `polymesh::` aliases; it MUST share the host Eigen target
because Eigen types cross its API and linking two Eigen versions is an ODR violation. The
tet10 mid-edge slot permutation between the two node orderings is a silent-wrong-answer trap
guarded only by the cross-backend solve test. Polymesh emits tet4/tet10 only; its hybrid/VEM
element families cannot fit `MeshOut`'s fixed-stride connectivity. Verification: core ctest
101/101; cantilever analytic tip deflection 0.2000 mm, Netgen 0.19270 mm, polymesh
0.19068 mm.

Then the adaptive solver, which is the reason the backend was worth having.
`sim/fea_adaptive.{h,cpp}` + the `sim_run_adaptive` RPC mesh, solve, score per-element
Zienkiewicz-Zhu error, refine and repeat to an error target, reusing `fea_run_static`
verbatim each pass by handing it an inline mesh — so BC resolution, bonded ties,
cancellation and the result payload cannot drift from an ordinary study. Surfaced as the
Simulation panel's adaptive controls plus convergence readout, and the `run_fea_adaptive`
MCP tool (registered in `advisor/triggers.{yaml,py}`, `sim_engineer.yaml`, the simulate
skill, `advisor/stages.py`, and BOTH `agent_turn.py` fabricated-success sets — miss those
and a model that ran only an adaptive study is told it ran none). Measured on a
120x60x8 plate with a 16 mm hole: eta 0.157 → 0.100 over three passes, 41.5k → 187.5k
elements, peak 170.0 MPa against a 56.8 MPa net-section nominal, i.e. SCF 2.99 vs the
classical Kirsch 3.

Two traps for whoever touches this next. First, `polymesh`'s graded-tet fill is a
three-level lattice (h, h/2, h/4): a size field alone only decides WHERE the fine cells
go, and on a part that is mostly boundary skin the fill is already saturated, so the
field changes nothing — measured, an identical 41,515 elements whether it asked for
4.87 mm or 1.96 mm. The background size is therefore the lever that guarantees progress;
the error field rides along as a bias. Second, and more embarrassing: this integration
first shipped the claim that polymesh "succeeds on geometry Netgen rejects" — in a GUI
tooltip, the sim role card, the simulate skill and an advisor finding — on no evidence.
Measuring five awkward solids found no case where polymesh meshed a part Netgen refused,
and one where the reverse held (two boxes fused along a single edge: Netgen meshes it,
polymesh's own watertightness guard refuses). Every one of those texts now describes
polymesh as an INDEPENDENT second discretization, which is what the measurement supports,
and `test_polymesh_backend.cpp` pins the edge-touch case so the claim cannot drift back.

**Public site (2026-08-16): tesseract brand hero + homepage restructure.** The
homepage hero is now the live brand mark: `src/scripts/tesseract.ts` renders the
tesseract in WebGL2 with the exact math of `host/src/ui/tesseract.cpp`
(DoubleRotation style, kSpin, kCamYaw/Pitch, fitStyle's d4/d3/depth/scale —
computed offline by the same sweeps and baked as constants, then verified over a
60k-sample trajectory sweep: pole margin 0.891, worst screen radius 0.9652 with
full camera wobble). 32 edges + 3x96 ring segments are instanced glow ribbons
(one-pass analytic bloom, no FBO), 16 vertex dots + the logo's 8-point spark are
billboard sprites, 240 dust points; two programs, three instanced draws, zero
dependencies, adaptive pixel budget like hero.ts. Two gotchas worth keeping:
(1) additive blending into a premultiplied canvas MUST write alpha =
accumulated intensity, not 1.0 — a constant alpha composites as opaque black
wherever the glow falls off (this rendered as black quads at every vertex);
(2) the part-builder panel needed its positioning UNLAYERED because
global.css's motion layer gives `[data-lift]` `position: relative` and later
layers beat earlier ones. The old SDF part hero moved down-page as a framed
showcase (`HeroCanvas.astro`, still the same renderer + `centered` option in
hero.ts that re-frames camera distance/uShift for a short box). index.astro:
hero copy slots into `TesseractHero.astro`; global.css: glass header, gradient
display text, gradient primary buttons, ambient body washes. Fallbacks kept:
prefers-reduced-motion parks the exact brand-pose frame (t=0), no-WebGL2 shows
the static mark; verified headless Chrome desktop/mobile/reduced-motion/
no-WebGL, zero console errors.

**Web (2026-08-19): the account system is the /admin door, and provider login
works in the browser.** Two owner-reported faults, one root cause each.
(1) `AuthMiddleware._authenticate` computed `cf_present` from the mere PRESENCE
of a `Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie and, when
present, skipped the `chudware_sid` branch entirely — so every browser that had
ever passed Cloudflare Access for the zone had its live session ignored on
EVERY request and was hard-401'd ("invalid access token") once that CF cookie
expired or stopped matching `cf_aud`, while holding a perfectly valid admin
session. `revalidate_websocket_user` had always preferred the session, so the
two disagreed. The session cookie is now tried first, unconditionally; Access
survives only as a fallback for a caller with no session. Proven by restoring
HEAD's `auth.py` under the new `tests/test_admin_account_door.py`: 4 of 6 cases
fail 401 there and pass now. The edge half is `cf_provision.py`, which no longer
provisions the `app.chudware.com/admin*` Access application or emits its AUD;
`verify_public.sh` now asserts the gateway's own `303 -> /signin?next=%2Fadmin`.
**The leftover Access application must be deleted by hand in Zero Trust** —
Access runs before the origin, so until then Cloudflare still answers /admin.
(2) Provider login in the WASM build had no working path at all. Every
credential surface funnelled into `drawOmpProviderManager`'s `agent_provider_*`
RPCs, which the gateway refuses for non-admins and which write into the ONE
shared daemon's bridge AuthStorage anyway; `-32003` is not `unknown_method`, so
`s_omp.available` stayed true and the legacy degrade section never drew. The
sound mechanism — the gateway's account-scoped `/api/providers/keys` (validate,
AES-256-GCM seal, resolve server-side for that account's turns) — had exactly
one client in the repo: the portal's own JS page. New `ui/web_provider_keys.{h,cpp}`
is an `emscripten_fetch` client for it (no-op off `__EMSCRIPTEN__`), Settings ->
Providers routes to `drawWebAccountProviderManager` on web, `providerUsable` /
`cloudCompatKeyPresent` / `moonshotKeyPresent` count an account key so a BYOK
user's models are selectable, and `drawValidatedCredentialHint` draws the form
instead of linking a locked door. Supported names are the six the gateway can
validate, and each IS the wire provider a turn carries — that identity is what
makes the stored key resolvable, so do not "tidy" it into a display name.
Subscription sign-in is now honestly closed on web: `subscription_login/logout/
switch/status/accounts/usage` are admin-only on the bridge
(`_OPERATOR_SUBSCRIPTION_METHODS`) because two of five providers use a fixed
server-loopback OAuth redirect a remote browser can never answer, and the status
RPCs leak the operator identity `providers.py` deliberately scrubs; the client
stops polling them under `webBuild()`. Verified in a real browser against a live
daemon: pasting a key into Settings -> Providers surfaced OpenAI's own 401
through the full WASM -> fetch -> gateway -> provider round trip.
Third fault, same session: the WASM client would not load on an iPhone in
Lockdown Mode (it worked on Android). Lockdown Mode disables WebAssembly in
Safari, and `host/wasm/shell.html` had NO capability preflight — the browser
downloaded ~12 MB and then either sat on "Loading Chudware…" or printed a raw
`Exception: …` from `window.onerror`, with `Module.onAbort`/`setStatus`
overwriting whatever was on screen. There is nothing to "fix" about Apple's
policy, but Lockdown Mode is excludable per site, so the honest failure IS the
deliverable. `shell.html` now preflights the three hard requirements —
WebAssembly, SharedArrayBuffer + cross-origin isolation (the build links
`-pthread -sPTHREAD_POOL_SIZE=8`), WebGL2 — names the missing one in a real
full-screen panel with selectable text, and on an Apple UA leads with the
per-site Lockdown exclusion steps. `setStatus` becomes a no-op once the panel is
up so the module's own churn cannot bury the diagnosis, and
`Module.instantiateWasm` is stubbed so the 11.6 MB `Chudware.wasm` is never
fetched at all. Verified headless with `WebAssembly` deleted before page scripts,
under both a desktop and an iPhone UA/viewport: panel renders, canvas hidden,
only `Chudware.js` and `Chudware.data` on the wire.

Suite note (pre-existing, fixed here because it hid the above): several gateway
fixtures did `config.settings = config.load_settings()`, replacing the singleton
that `auth`/`accounts`/`limits` had already bound `from config import settings`.
Whether a test saw the live object depended on pytest's file order, and the
symptoms were order-dependent rather than honest — `test_chat`'s WS broadcast
blocked FOREVER (`AuthMiddleware.__init__`'s no-strong-auth fail-fast firing
inside the message loop, so no frame was ever sent) and `test_auth_accounts_gate`
failed "no strong auth configured" only when `test_accounts` ran first. Both
reproduce on pristine HEAD from a two-file selection. `tests/conftest.py` now
offers `reload_settings`, which re-reads Settings from the environment and
monkeypatches it field-by-field ONTO the existing object; `test_accounts` and
`test_chat` use it and no longer pop `config` out of `sys.modules`. Do not
reintroduce a `config.settings = …` rebind in a fixture.

### 2026-08-19 — automatic release pipeline + dashboard-first /admin
Reworked /admin into a tabbed dashboard-first app (Dashboard/Releases/Accounts/
Terminal/System) and stood up the automatic release path end-to-end:
`.github/workflows/nightly-release.yml` (nightly-if-changed + dispatch) builds
Linux AppImage + Windows Inno on the self-hosted runners at a pinned
`CHUDWARE_VERSION_OVERRIDE`, runs ctest + a headless GUI smoke, signs with a
CI-held Ed25519 key (`release-ci-v1`; private seed in GitHub secrets, public
half in `core/security/release_keys.h`, the gateway keyring, and
`packaging/publish.py`), and publishes over the gateway LOOPBACK only
(`POST /api/releases/ci/publish`, bearer token in gateway.env, artifact
re-digested from disk before any trust). Beta rows carry a paired stable
signature; /admin's Releases tab promotes beta→stable in one click
(`POST /api/admin/releases/promote`, requires the full evidence bundle).
Updater got `requestCheckNow()` + a Settings "Check now" button, and
empty-channel 404 now reads "up to date" instead of HTTP noise. Gotchas:
`project(VERSION)` rejects prerelease — the override keeps MAJOR/MINOR/PATCH
numeric for cmake and puts the full string only in the version header;
`releases_version_canonical_check` (0011) forbade prerelease, so 0014 replaces
it allowing `-suffix` on beta only; `services/mcp/private_hostd.py` now sets
`CHUDWARE_MFG_STATE_DIR` and `start_new_session=True` for the spawned daemon
(a shared journal flock aborted it when any other hostd ran, and sharing the
test runner's process group delivered phantom SIGINT mid-suite).

### 2026-08-21 — first published release, and the defects it exposed
`0.1.0-beta.22` (linux/x86_64) is published, promoted to stable `0.1.0`, and
proven installable: `test_updater --live` drove the production check-and-stage
path against app.chudware.com and verified 340,724,216 bytes (size, SHA-256,
Ed25519, inner payload manifest) before the atomic stage. Getting there took
four fixes, each invisible until a real artifact existed:
(1) appimagetool REWRITES the AppDir's desktop entry while packing
(`X-AppImage-Version=$VERSION`, inserted after the group's last key), so the
shipped payload no longer matched the manifest signed before packing —
`stage_desktop_entry` now writes that key itself (a proven fixed point) and
finalize re-verifies the manifest AFTER packing;
(2) `local artifact="$1" request="$artifact…"` never works: the shell expands
every word before the builtin assigns any of them, so `$artifact` was unset and
`set -u` killed both finalizers at their last step;
(3) both finalizers delete the staged tree that held the only copy of
`payload-manifest.v1.{json,sig}`, so `finalize_release complete` aborted with
"found 0" and the published beta carried no evidence bundle (unpromotable) —
the pair is now retained beside the artifact;
(4) the updater could not consume its own manifest: the gateway omitted
`platform`/`arch` (which `manifestFromJson` requires), the artifact GET did not
send the entitlement the gateway's own artifact route demands (403), and the
inner-payload inspector execs the artifact while a streamed download lands 0644
("could not be safely inspected"). The credential now goes back only to the
origin that issued the manifest, and a credentialed download does not follow
redirects (libcurl and WinHTTP both replay custom headers across hops).
Also: each leg's failure reporter no longer depends on the helper the capability
step writes, so a leg that dies there stops reading as "queued" forever in
/admin.

### 2026-08-21 — all three platforms publish, and what stood in the way
`0.1.1-beta.32` published for linux, windows and macos x86_64 in one nightly and
was promoted to stable `0.1.1` for all three; the portal serves each. Every
blocker after the Linux one was platform-specific, and none of it was visible
before a real artifact existed:
(1) Windows never compiled: `core/util/platform_paths.cpp` defined
`WIN32_LEAN_AND_MEAN` while the CEF helper target already defines it on the
command line and compiles `/W4 /WX` (C4005 -> C2220);
(2) Nuitka refuses onefile mode on Windows without Dependency Walker and asks
interactively -- CI answers "no" -- so `build_sidecars.ps1` passes
`--assume-yes-for-downloads`;
(3) `os.stat()` and `os.fstat()` disagree on Windows for the SAME file:
st_dev/st_ino come from different APIs, st_ctime is a creation time NTFS tunnels
onto a replaced name (measured 14 minutes apart), and st_mode's executable bits
are a PATH heuristic os.fstat cannot apply. The payload TOCTOU guard compared
all of them and rejected every payload file; those three are now excluded from
PATH-vs-HANDLE comparisons on Windows only, handle-vs-handle stays strict, and
every mismatch message names the fields -- which is what turned each round trip
into a measurement instead of a guess;
(4) the Windows step signed a preimage name only the shell builders write;
(5) uploads died on **413**: the edge in front of the public origin caps bodies
at 100 MB, so the gateway now accepts `?offset=`/`&complete=1` chunks and both
off-LAN legs push 64 MiB at a time (proven against the live origin with a 220 MB
probe before it was wired in);
(6) macOS `installedPayloadRoot` looped forever: `for (q = p.parent_path();
!q.empty(); q = q.parent_path())` converges on "/" and "/" is never empty, so
ANY executable outside a .app bundle spun in place -- a shipped-app startup
hang, found because host test_security burned a whole ctest timeout;
(7) `chudware_read_version` used GNU-only `\+` in sed, so BSD sed read no
version and every macOS packaging step reported a missing header;
(8) onnxruntime-node 1.26.0 ships no darwin/x64 prebuild at all, so Intel macOS
leaves it `--external` (allow-unresolved matches module specifiers, not the
relative path that package computes);
(9) mesher_advanced's 90s wall ceiling is real on a 2017 iMac (114s) --
CHUDWARE_TEST_WALL_SCALE relaxes it on that runner only.
ctest now runs with `--timeout 900` everywhere: an unbounded stall used to burn
the job's whole 6-hour budget and, with cancel-in-progress, every nightly behind
it. macos/arm64 stays unproven only because `macbook-air-m2` is asleep.

### 2026-08-22 — organizations, teams, and customer file destinations
The web gateway now models organizations, organization memberships and invitations,
teams, and team memberships in PostgreSQL (`migrations/0015_organizations.sql`,
`orgs.py`), with owner-funded shared storage, opaque tenant authorization, and
single-use invite links. `bucket.py` keeps personal files private while adding
explicit team-scoped roots; the portal and bucket assets were rebuilt as
content-hashed, accessible destination surfaces with real organization/team
management and upload/rename/delete flows. Full gateway coverage is green (615
passed, 4 skipped), including cross-team isolation and UI asset contracts. Billing
remains deliberately account-owned: an organization derives shared-storage
entitlement and quota from its active owner rather than introducing a second
Stripe customer model.

### 2026-08-25 — T-DP14 design-pipeline history wiring

The four typed design-pipeline exits now call the existing PLM bridge after
their ledger exit event is durable, producing one deterministic Project History
narrative from each stage-local `feature`, `geometry_spec`, `linkage`, or
`build_node` slice. Multi-part boundaries carry an item-to-buildgraph-node map
through immediate and queued checkpoints; the vault writes the mapped node into
each item's AI metadata so per-item timelines retain the producing participant.
Touched `services/agent/{pipeline/phases.py,plm_bridge.py}`,
`services/plm/{client.py,vault.py}`, their focused tests, and the additive
design-pipeline/PLM contract notes. The bridge remains best-effort and managed
mode only; a vault failure queues metadata without blocking mission progress.

### 2026-08-26 — portable SolidWorks parts/assemblies into native FEA workflow

Modern SolidWorks 2015+ `.SLDPRT`/`.SLDASM` import no longer requires a Windows
SolidWorks installation: `core/doc/solidworks_tessellation.*` decodes saved-display
streams, reconstructs the exact triangle-strip topology, composes nested assembly
instance transforms, and shares distinct component meshes before `formats.cpp` sews
them into baked native B-rep import bodies. `session.cpp` still prefers COM→STEP for
exact analytic surfaces, persists flattened component paths plus explicit
`saved_display_tessellation` fidelity in `.step+`, and the MCP surface falls through
to this portable lane when feature-tree COM extraction is unavailable. The Python
format oracle mirrors the codec. Focused synthetic tests cover source-independent
STEP+ reopen, two-instance assembly placement, and Polyhedral-FEA meshing; the real
Crimson Comet corpus parsed 35/35 native files (4 assemblies), real `Tread.SLDPRT`
and `Wheel.SLDASM` imported through `libchudware`, and an imported
`Axle Rod.SLDPRT` completed a 1 kN PolyMesh static solve.

## 2026-08-26 — Box.com mirror integration (`aicad.box_mirror/1`)

Optional cloud MIRROR of saved project files on Box.com; live edits stay local.
New `services/plm/box_mirror.py` (stdlib-only Box REST client, durable per-project
spool under appdata `box_mirror/`, sha1 change detection, version-preserving
uploads, OAuth refresh-token rotation persisted under `config_lock`, loopback
OAuth connect at `http://127.0.0.1:53682/box`). Hooks: post-`Vault.save` commit
(`services/plm/vault.py`), verified `_project_save` + bounded terminal-sweep drain
(`services/mcp/cad_common.py` — the fresh-per-turn agent worker must drain
blocking; the spool survives kills). Tools `box_sync`/`box_status` registered in
the plm family (`services/mcp/plm_tools.py`, pack in `tool_categories.py`,
`box_sync` risk-overridden EXTERNAL in `tool_registry.py`). GUI: worker RPCs
`box_status/box_sync/box_notify_save/box_oauth_login` (`services/plm/worker_rpc.py`),
Settings → General → "Box.com mirror" section, `boxMirrorNotifySave()` beside every
`plmAutoRecordSave` call plus the async `doSaveDoc` success callback (host/src).
Contract: `contracts/box_mirror.md`. Config ownership gotcha: the GUI must
nested-overlay `box`/`box.auth` — Python owns the ROTATING `access_token`/
`refresh_token`/`access_token_expires_at` (Box refresh tokens are single-use; a
clobber revokes the grant). Tests: `services/agent/test_box_mirror.py` (fake
transport, 16 tests). Verified live against a real Box account end-to-end
(upload, version update, dot-dir exclusion, loopback OAuth connect).

### 2026-08-26 — agent harness integrity and cost-control cutover

The universal in-app agent lane now preserves an absent desktop billing actor as
JSON null instead of manufacturing `{}`, classifies reached-worker failures as
`worker_error`, hoists actionable terminal detail, and preserves partial/refine-cap
outcomes. `agent_turn` now applies HARD finish guards to synthetic handoffs,
requires fresh post-mutation verification evidence, fails closed on guard errors,
keeps sketch-role products intentional, distinguishes attempted from completed
delegate work, and enforces aggregate token grants. Vendored OMP is the sole
empty-completion retry owner; failed OMP responses are metered, failed delegates
accrue spend, provider tiers reach live schema rendering, and delegate schemas
render once. Dead `adopt_role`/`request_role_change` prompt, config, dispatcher,
and Settings surfaces were removed in favor of exact catalog role IDs through
`delegate`; unknown roles fail before allocating a child. Terminal document saves
now run once and report failures as partial delivery. Regression coverage spans
actor forwarding, worker classification, token ceilings, retry disposition,
guard ordering, persistence, canonical roles, and terminal honesty; generated
tool-panel tables were refreshed.

### 2026-08-26 — manufacturing drawing and model-delivery quality cutover

Press-project forensics traced unusable shop output to four connected seams: render
tools were reduced to image-path text before the model's next turn, feature-target
diameter picks used ambiguous concentric centres, vector drawing export auto-cropped
away the requested paper/title-block semantics, and `set_material` changed appearance
while native documents silently retained default 6061. `services/agent/agent_turn.py`
now feeds real result images back to vision providers and requires a current final
drawing review before export. Drawing MCP tools now pair pixels with exact layout and
material diagnostics, support free-text notes plus in-place annotation edit/delete,
and select the requested concentric ring. Core drawing layout reserves a title-block
band, compresses unused grid columns, renders/reviews the full sheet, and exports fixed
ISO paper with a border and framed title block. Named material assignment now stores
the engineering registry identity alongside appearance; role/playbook/tool guidance
requires mission routing for multi-part shop work, explicit material, domain-specific
verification, final reviewed drawings, native `.step+` plus neutral `.step` delivery.
Regression coverage pins image feedback, export review freshness, concentric diameters,
annotation repair, material consistency, occupied-grid layout, fixed A3 MediaBox, and
title-block vector cells. A representative stepped-punch workflow produced clean
9.8/20 mm diameter callouts, 95 mm OAL, zero review warnings, A3 PDF, native `.step+`,
and neutral STEP. Existing `/home/hunter/Desktop/press` artifacts remain historical and
must not be sent to a shop without regeneration through the corrected workflow.

## 2026-08-26 — agent harness: refusals are data, not failures

Triggered by a live card reading `tool 'research_web_search' exceeded
max_calls_per_turn=3 (agent.tool_options)` — a red "failed" tool card that named a
config key where a next action belonged. A four-slice read-only audit found the same
shape everywhere: **the harness refuses a call, then reports the refusal as if the
tool had crashed.** Clean-HEAD `services/agent` went 37 failures → 1 (the remaining
one is a deliberate open decision, below).

**One taxonomy, end to end.** `agent_turn._classify_tool_outcome` returns
`(outcome_kind, executed)` over `TOOL_OUTCOME_KINDS` = ok / policy_refusal /
guard_block / validation_error / timeout_unknown / internal_error / tool_error.
`agent_events.tool_call_event` carries both (omitted when unclassified, so older
consumers keep ok-only behaviour), and `host/src/ui/copilot/{feed,activity_timeline}.cpp`
render each class distinctly via the new `tool_outcome.h` — deleting the `"loop guard:"`
prose match that used to be the only way the GUI could tell a refusal from a crash.
`executed` answers exactly one question: did the tool BODY run, so could state have
changed? A timeout reports `executed=True` because the abandoned thread is still
running.

**Consequences the callers depend on.** A refusal no longer: spends the turn's
execution budget (`tool_calls_made` is charged only inside the `_blocked is None`
branch, with a separate bounded `_refusal_ceiling` so a wedged guard still cannot
spin); files a bug report; or halts queued directives — that halt is now gated on
`executed`, because a call that never reached the tool body invalidated no state, and
halting used to delete the exact remedy the refusal had just named. Allowlist denials
also now EMIT a tool event; they previously bypassed `tool_call_event` entirely, so
the user watched the model stall with no card at all.

**The per-tool cap specifically.** `config.effective_tool_call_cap` is now the single
resolver shared by enforcement (`_dispatch_tool`) and ADVERTISEMENT
(`tool_schemas.render_tools_block` renders `Per-turn limit: at most N call(s)`), so
the model can plan around a cap it can finally see. The counter is charged AFTER the
call and only when it reached the tool body, so malformed calls can no longer retire a
tool the model never used once; the call that spends the last unit carries a heads-up
note; and the refusal itself is a `call_cap_reached` payload carrying the tool's own
curated `prompt_note` as the remedy. `research_web_search` stays at 3 — a sweep of
every role card confirmed no role's prose mandates more than three searches.
Settings-disabled tools are dropped from the rendered schema entirely rather than
advertised and then refused.

**Two dispatch paths, one policy.** `copilot_mode_tools.apply_meta_tool_floor` is now
applied by `agent_turn`, `mission_dispatch` AND `node_runner` before schema rendering.
Path B never applied it, so every compiled DAG node ran confined to its 14 card tools:
`enable_tools` was callable but INVISIBLE. Same for budget —
`tool_budget.completion_cap_for_subagent` gives both lanes the same 8192 headroom (B
silently took `agent_turn`'s 2048 default when a node had no tier), and
`tool_budget.ROLE_MIN_CALLS` raises the soft convergence floor to each role's audited
mandated chain (ecad_engineer 35, model3d 24, feature_modeler/spec_writer 18 — all
were being told to "converge now" at 16, before their own card's chain could finish).
`role_models.clamp_provider_effort` is applied to the FINAL provider on every path out
of selection in both lanes; the session-default branch discarded the clamped config and
reused the ambient provider, leaking `max` effort past the `high` ceiling.

**Honest terminals.** A twice-truncated reply is `reply_truncated` (retryable in
`node_runner._RETRY_REASONS`), not `no_code` — it is a provider output cap, not model
inaction, and `no_code` both blamed the model and blocked retry. A done-gate that
cannot RUN returns `GUARD_INTERNAL_PREFIX` and routes to `guardrail_internal_error`
instead of handing the model our ImportError forever as `guardrail_blocked`. The WI-10
refine cap is enforced BEFORE the rework dispatches, so the revision the turn keeps is
genuinely the last settled one; it used to fire after the mutator had already dirtied
the geometry while reporting "the verified build is kept". `_PRODUCTIVE_TOOLS` now
unions `_DELIVERED_PRODUCT_TOOLS` (a cam_programmer that generated a real toolpath
counted as having written nothing), and a geometry-producer `_ARTIFACT_PRODUCT_GATES`
entry stops a `feature_modeler` being synthesised as `done` after only verifying.
Every artifact gate now also checks the lane can actually CALL the product tool it
demands — that check is what keeps these gates satisfiable.

**Recovered work.** `_drop_oldest_tool_results` REPLACES trimmed results with a
placeholder instead of deleting them: deletion left orphaned assistant messages, and
strict-alternation providers answered the overflow retry with a 400 that
`classify_provider_error` reported as `provider_auth` — sending users to check
credentials that were fine. A 404 "no endpoints" is now non-retryable (it burned every
DAG retry). `_attach_arg_notes` wraps a non-envelope result instead of returning it
unchanged and dropping the note. The delta coalescer is flushed in `run_turn`'s
`finally`, so the last thing a model said before a provider error still reaches the
transcript. `enable_tools` is transactional — schemas render and registry policy
applies against a CANDIDATE set before `allow` is widened, so a green card can no
longer be followed by "not allowed". Sub-agent termination is read and acted on in its
own guarded block: a registry write that threw after the flag was set used to skip the
return and keep dispatching tools while Stop looked accepted.

**Also found and fixed, same session.** `providers/token_usage.record_usage`
incremented `usage_event_count` only when a worker-RPC sink was bound, so on every
normal desktop run `agent_turn` classified each completion as unreported, wrote a zero
row, and the GUI's context/token gauges were SUPPRESSED ENTIRELY. `geometry_helpers`
hard-required `libchudware.so` and raised, so all process gating and provenance died
on an unprovisioned box — it now mirrors the six authoritative DfM profile rows
(verified 6/6 equal to live `dfm_get_profile`) and the bundled material registry.
`sim_engineer.yaml` carried 15 tools against the hard cap of 14, silently losing its
trailing entry. `test_cheatsheet_examples` leaked open engine documents into every
later test in the process. Loop-guard exemptions now cover every real status poller
(the third identical `sim_status`/`fw_status`/`scrape_queue_status` was refused) and
`_canonicalize` no longer strips `coordinate_claim`'s genuine `intent` argument.
`guardrails.checkpoint_verdict` is shared by both checkpoint paths so the capability-A
aggressive path can actually reach its retry escape. The manufacturing guidance family
is authored (15 entries) and the Tools-panel tables regenerated; a new gate pins
snapshot guidance CONTENT, not just tool names — seven entries had drifted into
materially false advice.

**Deferred, deliberately.** `Webservice/gateway/provider_accounts.py` genuinely
violates `test_prohibited_auth.py::test_no_consumer_oauth_token_reuse`: it exposes
`https://claude.ai/oauth/authorize` and posts grants to the consumer token endpoint for
OTHER tenants' accounts. That gate is red at HEAD and stays red — removing the Claude
Pro/Max subscription sign-in is a 212-line product and billing decision, not a harness
fix, so it is surfaced rather than taken. The failure message now names both source
lines and the two accepted remediations. Also unresolved and environmental: the
`services/mcp` ECAD/FEA/CFD/hostd suites need a live engine and daemon.

## 2026-08-26 — drawing overhaul: from nominal sheets to machinable packages

Trigger: the press package. Six one-page PDFs with bare nominal dimensions, a
`4X DIA 5` where `4X M6×1.0 ⌵14` belonged, every tolerance/finish/weld exiled
to a Markdown disclaimer, and a review gate that called it "manufacturing_
ready" because the LAYOUT was clean. The audit found three strata and all
three were repaired: the renderer lacked primitives (text-only angular dims,
leaderless notes, no tables/multi-sheet), the engine had capability no tool
exposed (smart tapped callouts, balloons, BOM rows, weld symbols, datum
targets), and the pipeline never demanded completeness (auto-dimension
preferred, coverage never required, release gated on pixels).

Engine: multi-sheet drawings with export-all (multi-page PDF, side-by-side
DXF); first-class BOM/revision/hole/notes/cut-list tables feeding raster and
vector alike; real angular arcs, note leaders, chamfer callouts, ordinate
sets, full AWS weld symbols, material-aware hatch; title block extended with
checked/approved, drawing standard, projection glyph, general tolerance,
mass; jogged multi-plane sections refuse rather than silently approximating.
Tools: hole callouts auto-populated from Hole features, tolerances at
dimension creation, composite GD&T, sheets/tables, and audit_drawing — a
spec-to-annotation coverage matrix plus a standards category (title
completeness, GD&T datum resolution, thread grammar, dangling annotations);
release_ready requires full critical coverage AND zero standards failures.
review_drawing no longer claims manufacturing readiness from layout.
Pipeline: drawing_detailer hands off a coverage matrix; spec_writer owns the
machined-part checklist; missions expand a manufacturing package into part
drawings + assembly drawing + BOM/cut list + work instruction + weldment
traveler. Formats: standalone editable .draw+ (aicad.draw+/1, embedded
source, honest missing-source state), SVG exports with real text and layer
groups, PDF companions=[dxf,svg], ODT/ODP/ODS + legacy PPT/XLS reading,
office_convert, structural DOCX edits. Deliverables: work_instruction and
weldment_traveler templates (provenance-backed, signoff blocks).

Proved live in-process: box + bore + M6 tap → drawing → tapped callout
"M6x1.0 ⌵ 14" → audit passes coverage, BLOCKS release on genuinely missing
projection/standard/general-tolerance title fields → PDF/DXF/SVG all carry
the thread text.

## 2026-08-26 — manufacturing planner, machining reference, standards floor

The agents now THINK about manufacturing before they model. `mfg_plan` is the
fifth typed ledger kind: process with resolved DfM profile snapshot, stock,
datum/setup strategy, ordered operations, inspection plan, and CRITICAL
DIMENSIONS derived from the part's function — every numeric row carries
required provenance and is audit-native, so `mfg_plan_get(view="audit_spec")`
feeds `audit_drawing` byte-for-byte and `view="work_instructions"` projects
straight into `make_work_instruction`. `mfg_plan_check` is three-state; an
empty evaluation can never read as pass (the same fix landed in dfm_check,
which also gained min-feature, hole-aspect, and thread-strategy checks). A
new manufacturing_planner role runs between SPECIFY and BUILD for machined/
fabricated deliverables; spec_writer, feature_modeler, drawing_detailer,
cam_programmer and fixer all read `mfg_plans` and follow the plan → model →
check → audit → instruction chain.

`machining_ref_search/get` is the Machinery's-Handbook-class reference: a
curated dataset where every row cites its governing standard (ISO 2768
general tolerances, IT-by-operation capability, ream/bore allowances,
finish-by-process, drill series, counterbores per B18.3, thread engagement,
keyways, press-fit and heat-treat/grind stock guidance) and queries about
facts that already have canonical calculators DELEGATE to them rather than
duplicating tables. `machining_ref_index_pdf/search_pdf` index the user's own
handbook PDF locally (per-page FTS5, page-cited snippets, nothing
redistributed) — drop a PDF in and cite it by page. Fixing the reference
layer also fixed a real defect: the fit catalog's own H7/m6 bearing-seat
recommendation was uncomputable because ISO 286 shaft `m` was missing; every
recommendation row is now pinned computable.

## 2026-08-26 — electrical schematic subsystem: connectivity truth, deliverables, editor

A four-scout audit of the ECAD schematic stack found the engine computing the
wrong answer and the surfaces around it unable to reach it. Connectivity keyed the
union-find on a bare `Point{x,y}`, so two hierarchical sheets whose geometry happened
to coincide were electrically shorted; a pin landing on a wire's *interior* floated
(only exact vertex coincidence united); local labels were emitted under their bare
text, merging same-named nets across sheets (the code warned they were
"shorted-by-name"); buses were parse-only; and multi-unit parts emitted one component
per unit, so U1A/U1B became two BOM lines and two footprints. ERC compounded it:
`passive` counted as a driver so `no_driver` almost never fired, ANY power port
counted as a power source so a plain GND symbol silently satisfied
`power_not_driven`, and `no_connect_connected` required a second pin so an NC flag on
a wired stub went unreported. All of that is fixed: the key is `(sheet, snapped
point)`, endpoints attach to segment interiors (explicit junctions still required for
true crossings), classes carry stable ids with `@sheet` disambiguation, buses/bus
entries carry real member connectivity, units group into one component, and ERC uses
KiCad drive levels with `PWR_FLAG` as the only power source. `netlist.cpp` now emits
`net_info`, `buses` and `by_object` (entity id → net) — the last is what finally lets
the canvas highlight a net without re-deriving connectivity.

The subsystem could also produce **nothing**: `ecad_export` rejected every kind that
was not `gerber|drill|bom|cpl` and each required a pcb doc, so a schematic had no
netlist, no BOM, no SPICE deck, no KiCad output and no printable sheet. New
`core/ecad/schexport.cpp` (netlist s-expr / BOM / SPICE) and `core/ecad/schplot.cpp`
(SVG + a self-contained vector PDF sharing ONE `Painter` so the two can never drift)
close that, and `ecad_export` now honors `path` and returns `paths` — it previously
ignored `path`, wrote nothing, and the GUI toasted "Exported" unconditionally.
`core/ecad/symlib.cpp` adds the native symbol library the C++ core never had (78
symbols); before it, `sch_add_symbol` fabricated a generic 2-pin body for every libid.
The op vocabulary went 42 → 62 names so every entity the schema can hold can also be
edited and deleted, `sch_import_kicad` stopped being a registered no-op, and
`ecad_design` — which the GUI called on every AI Design click and which **nothing
served** — is now a native compiler from `recipes.h` to a real, wired, ERC-checked
schematic.

Two defects only showed up under verification, and both are worth remembering.
`sch_connect` routed pin-to-pin straight through the source symbol's body; with the
old netlist that was invisible, but once pins attach to wire interiors it became a
genuine short (wiring U1.3 to C1.1 across a QFP48 also caught pin 34 at the same y).
The router now escapes each pin along its own direction and rejects any candidate path
that touches a pin it was not asked to connect. And the new on-canvas sheet frame was
anchored at the origin extending into −y while content lives in +y, so zoom-to-fit
unioned two disjoint regions and crushed the schematic into an unreadable clump — the
page is now anchored around the content bbox exactly as the plotter does it.

Deferred, deliberately: `sim_run`/`sim_cancel{run_id}` in `contracts/ecad/ecad_rpc.md`
still describe an isolated circuit-sim worker that does not exist (the shipped path is
synchronous `ecad_simulate` + mechanical `sim_cancel{job_id}`); that divergence is now
recorded in the contract rather than papered over. `ecad_to_3d` still ignores
`into_doc_id` and returns no `part_doc_id` — board→3D, out of scope for this pass.

## 2026-08-27 — hostd exits when the app does; one autosave authority
Symptom: a tray icon reading `Chudware hostd — draining (1 client)` that never went away.
Root cause: every exit path (idle-exit AND drain) waited on `status_.clients`, so ONE
connected-but-silent socket vetoed shutdown forever — measured live at 5.5 h, the "client"
being a parked tooling connection with no attachments and no traffic. `Server::idleExpired`
and the drain now gate on `Server::pinningClients(idleWindowSecs)`: a `gui` client always
pins, any other client pins only while it has an engine call in flight, an attached doc, or
an RPC inside the idle window (`ClientConn::lastRpcMs`, `DocRegistry::hasAttachments`).
`Server::beginDrain(deadlineSecs)` replaces the bare `status_.draining = true`: explicit
stops (`daemon_drain`, `--stop`, tray Quit, SIGTERM) are HARD and carry a deadline
(`CHUDWARE_HOSTD_DRAIN_GRACE_S`, default 20 s; tray Quit 5 s) after which stragglers are
`shutdown()` and the daemon exits; the stale-binary self-drain stays soft (deadline 0). The
doc-GC grace no longer pins idle-exit at all — with the 1800 s agent floor it kept hostd and
its tray icon alive for half an hour after the GUI closed, protecting a doc nothing could
open (the check is only reached when nothing is attached). Clean exit still discards; a
grace expiring while the daemon serves is still retired by `tick()`. Files:
`core/hostd/{server.h,server.cpp,doc_registry.h,doc_registry.cpp,main.cpp}`. Also removed
`Autosave::stop`'s dead `finalSave` parameter (its body already `(void)`-ed it).

CEF fetch daemon: 193 orphaned `chudware --cef-daemon` processes were alive 34 h after
every spawner died, sockets already unlinked. Its only self-termination was an idle check
inside the single thread that also drives `CefDoMessageLoopWork` — and `initialize()` can
block on Chromium's ProcessSingleton before that loop is ever reached, so a wedged instance
lived forever while each later turn spawned another. `cef_daemon.cpp` now arms
`start_idle_watchdog()` BEFORE `initialize()` (own thread, `_Exit(0)` after `idle_secs` of
no `mark_activity()`), and `services/browser/cef_client.py::_spawn_daemon_detached` is
single-flight (flock + re-probe under the lock) so a fresh per-turn worker adopts an
existing daemon instead of minting a rival.

Autosave de-duplication: `cad_common._autosave()` was writing an MCP-side snapshot on ~7
per-tool call sites with no daemon-mode guard, while its two aggregate callers each carried
their own copy of that guard. In daemon mode that write repoints the live doc's `path`
outside hostd's recovery tier and clears `dirty`, defeating `gcCloseDoc`'s
retire-if-unsaved gate — an abandoned agent doc could be closed without ever reaching the
orphan tier. The guard now sits at the single write; the duplicate in `_autosave_all_open`
is reduced to a cheap `doc_list` skip. Deleted `prune_autosave_debris` (an opt-in
maintenance sweep with no tool entry point — its own docstring said "never called by any
tool"), its `cad_tools` re-export and its test.

Tests: `services/mcp/test_hostd_registry.py` gains `test_parked_client_does_not_pin_the_daemon`,
its control `test_attached_client_still_pins_the_daemon`, and
`test_drain_exits_on_its_deadline_despite_a_stuck_client`. The two bug-defending ones were
A/B'd against a HEAD-pinned hostd built out-of-tree (`/tmp/headhostd`) and fail there.
Gotcha for the next pass: `test_inprocess_internal_agent_docs_are_registered_but_silent`
fails on HEAD too — it is the concurrent ECAD session's in-progress `core/api/libchudware.cpp`,
not this work.

## 2026-08-27 — ECAD: the gaps the schematic pass left open

Four scouts re-audited the ECAD surface after the schematic overhaul and found the
remaining defects were mostly *reachability*, not missing capability — working engine
code that nothing could call, and one predicate that swallowed calls it had no
implementation for.

The worst was in `services/worker/worker_client.cpp`. `routeEcadToCpp` claimed a call
for the C++ core whenever the method began with `ecad_` and the doc id began with
`ecadcpp`, with **no check that the core implements that method** — unlike the sibling
`kDocless[]` branch, which is properly allowlisted. Since `cppEcadFlag()` now
unconditionally returns true, every board is C++-backed, so every MCP-only `ecad_*`
tool carrying a doc id was captured and died on the core's `unknown method`
fallthrough. That envelope carries `error`, not `code`, so the caller's
`result.contains("code")` test missed it and the call hard-failed *before* reaching the
generic Python/MCP bridge. The GUI's "Send board to thermal FEA" button therefore
failed 100% of the time on every board on every platform, with the real implementation
sitting in `services/mcp/ecad_thermal.py` the whole time. 40 doc-bound MCP tools were
in that blast radius. The comment justifying the over-claim ("there is no Python ECAD
worker left to fall back to") confused the deleted Python ECAD *worker* with the very
much alive Python MCP *tool surface*. Both lists are now explicit and their disjoint
union is exactly the 28-method dispatch table.

`core/ecad/drc.cpp` had the same shape of bug the schematic ERC had: checks that pass
because their input never arrives. `track_shape()` returned false for any track whose
`kind` was not `"segment"`, so every arc track vanished from `copper_shapes()` — and
because `copper_shapes()` is also the autorouter's obstacle source and zone-fill's
keepout source, the router would happily lay a track across an arc of another net and a
pour would flood over it. DRC could not merely miss a short; the tools could *create*
one it then could not see. Arcs now tessellate into capsule chains with a sagitta
margin so the swept copper can never be under-approximated. Filled zone copper was
absent from the DRC model entirely (fill-time clearance goes stale the moment a
footprint moves), and net classes were honored for clearance but not for `track_width`
or via sizing, so a track thinner than its own class minimum passed clean. Nine of the
eleven new `test_drc_geometry` cases fail against the old engine; the two that pass are
the deliberate false-positive guards.

`services/mcp/ecad_sch_tools.py`'s `_pcb_status` swallowed a DRC compile exception and
reported `0 DRC error(s)` — the exact defect fixed for schematic ERC one commit
earlier, still live in the PCB twin, on the call agents are told is "THE situational-
awareness call".

The SPICE engine turned out to be real and substantial, and completely uncancellable:
`sim_cancel {job_id}` was a working registry whose only consumer was the LBM wind
tunnel, so cancelling a circuit sim was a guaranteed no-op returning
`cancelled:false`. The work is genuinely unbounded (transients reach ~4M iterations;
Monte-Carlo allows 100k full re-analyses) and the GUI called it synchronously from the
frame loop, so a heavy study froze the whole application. The solver now polls a token
at every unbounded loop head and the GUI runs it off-thread with a Cancel button. A
300-run Monte-Carlo study that would have taken ~9 minutes now stops **35 ms** after
the cancel, returning `ok:false, cancelled:true` with its partial run count.

Also closed: `ecad_to_3d` honors `into_doc_id` and returns `part_doc_id` (it mints the
part doc via the already-shipped `doc_import_file`, so a re-export updates the existing
3D tab instead of spawning another anonymous one, and a bad id degrades to a warning
without erasing a successful STEP); `led_driver` is ported to `core/ecad/recipes.cpp`,
so native `ecad_design` can finally build one and the orchestrator stops bypassing the
RPC; `ecad_route_diff_pair` and `ecad_load_symbol_library` — both working, both with
zero callers — are now exposed as MCP tools; the PCB canvas paints persistent DRC
markers like the schematic already did; and the fictional `sim_run`/`sim_cancel{run_id}`
declaration is deleted from the contract in favour of what ships. `all_recipes()` was
dead in both languages and is gone.

## 2026-08-27 — PLM team sharing and advisory-lock completion (T-R7)

Completed the existing contract-first remote stack in `services/plm/remote.py`, `vault.py`, worker RPC, and MCP tools: fixed plain-language transport failures with raw detail restricted to logs, best-effort live lock status, expiry, explicit one-confirm force override, reserved account identity, and forward-compatible snapshot-sync/advisory-lock capabilities. The Project Hub now has a real Team & Sync destination, folder/server share dialog, per-remote sync results, live lock rows, and an active-document lock chip/action path. Frozen `contracts/plm.md` §6.4 remains authoritative: divergent edits are automatically preserved as copy-as-new-item with no merge-choice UI; Box mirror remains separate. Verified with 388 PLM tests, isolated/UI compilation, scoped GUI lints, an integrated host build, and a real remote-profile Team headless capture. Concurrent ECAD/public-site/hostd/browser work was excluded.

## 2026-08-27 — self-hosted PLM live collaboration (T-R8)

Added the frozen `aicad.plm-live/1` authoritative relay after an OSS survey rejected CRDT/OT duplication of Chudware's semantic engine order. `services/plm/server.py` and `live_client.py` provide authenticated total-order batches, canonical snapshot catch-up, bounded presence, reconnect idempotency, local-hostd atomic application, and one participant-attributed final checkpoint; worker/MCP live methods and `plm-server` profiles select the mode without changing T-R7 dumb-remotes. `WorkerClient` transparently reroutes joined documents' ordinary `doc_apply_ops` to the live proposal seam, never falling back locally after refusal. Project Hub/quickbar render live-server join/leave, LIVE/reconnecting, and participant state while retaining advisory locks for folder/SSH/HTTPS profiles. Proof: actual clients observed sequences 1/2 and equal final hashes, one checkpoint named both committers, core replay passed `eval_compare`, 413 PLM tests passed, native and WASM builds passed, all 103 core CTests passed, the 9,627-test full gate returned PASS, scoped GUI lints were clean, and a live-server Team headless capture wrote 1340x838. Concurrent marketplace/ECAD/public-site/hostd/browser work was excluded.

## 2026-08-27 — agent harness: limits calibrated against what they bound

Triggered by a live card reading `tool 'research_web_search' exceeded
max_calls_per_turn=3 (agent.tool_options)` mid-research, with the model's own next
thought being "I could fetch more technical references". The 2026-08-26 session had
already fixed that message's PROSE; this session found the defect class underneath it:
**a ceiling calibrated without reference to what the wrapped mechanism actually takes.**
Four read-only audits (refusal surface, turn budgets, guardrail satisfiability,
timeouts/retries) plus four repair slices. `services/agent` + `services/dag`: 3809 pass,
2 fail, both pre-existing (below).

**The unit mismatch.** `max_calls_per_turn` is enforced against a counter created once per
`run_turn`, i.e. per whole USER REQUEST, not per model round-trip. `research_web_search`'s
3 was reasoned per-round-trip ("three searches is enough to triangulate"), so it meant
three web searches for an entire engineering task. Now 24, with the RUN scope stated at the
entry and the arithmetic given (~9% of `MAIN_CHAT_RUNAWAY_TOOL_CALL_BACKSTOP` = 256, an
order of magnitude above any observed legitimate chain, still bounding a rephrase spin at
24 paid searches instead of 250). Identical repeated queries are the loop guard's job; this
counter counts by tool NAME and cannot tell a new subtopic from a retry.

**Refusals: bounded, not free.** `_refusals_made` claimed in its own stop text to be
"consecutive … without a single accepted call" while being a lifetime total that was never
reset, so twelve single-shot guard corrections scattered across a 256-call run forced a
stop. It is now genuinely consecutive (reset by every accepted dispatch) and DISPATCH-time
refusals (per-turn cap, loop guard, Settings kill-switch) are charged to it — at HEAD only
PRE-dispatch blocks were, so an identical-call spin had no bound short of the runaway
ceiling. Measured by neutering the fix: **256 paid provider round-trips**, reported as
`main_chat_runaway_backstop`; with it, ~14 and an honest `refusal_ceiling`. The ceiling is
checked at the TOP of the loop so the refusal that trips it still emits its card — the stop
text promises "the last refusal text names what it demanded", and breaking at the increment
discarded exactly that. A REFUND of `tool_calls_made` was tried and reverted: that counter
is simultaneously the runaway bound and the reported `tool_calls`, so refunding made
refusals free AND unbounded (41 round-trips against a nominal 20-call ceiling, honest stop
replaced by `no_code`). A tighter bound is the fix; a free retry is not.

**Remedies must name tools the lane holds.** `_empty_product_handoff_block` OR-ed an
actor-NAME substring (`"ecad" in actor_l`) as an alternative to its tool-availability check,
so `ecad_engineer` — 14/14 tools, none of them board tools, no widening path — was told to
finish through `board_from_schematic` → `ecad_run_drc` and could only earn allowlist
denials. Both name-based branches now gate purely on the GRANT, with a schematic-only lane
that ends at `ecad_netlist`/`ecad_bom`/`ecad_plot`, the artifacts its own card mandates;
those three also joined `_DELIVERED_PRODUCT_TOOLS`, which had made a legitimate ECAD finish
read as zero delivered product.

**Budgets vs mandated chains.** `ROLE_MIN_CALLS` gained `reviewer: 24` (AC1-AC9 grading plus
its card's standard battery; it holds no `PRODUCER_TOOLS` and its real phase `deliver` misses
`BUILD_PHASES`, so all three producer signals missed the one role that decides PASS/FAIL —
and `reviewer_exec` treats `not_checked` as first-class, so early convergence yields
honest-looking NOT_CHECKED rows rather than a loud failure), `drawing_detailer: 24`,
`cam_programmer: 22`; `PRODUCER_MIN_CALLS` 16 → 20. `MAX_TOOLS_PER_ROLE` 14 → 16: it carried
no rationale at all and 13 of 32 cards sat exactly on it, so it had stopped bounding cards
and started designing them — `ecad_engineer` was ordered by UNIVERSAL RULE 3 to
`ledger_note` every artifact while holding no `ledger_note` seat. Verified zero cards were
losing tools to truncation before raising, so no existing grant changed.
`render_schematic_ascii` left that card: it is a GSL script function, never a registered
tool, and the loader reported it unresolvable.

**Two lanes, one policy.** `delegate` never read the `result['retryable']` flag the DAG lane
has always used, so a transient 429/5xx/truncation killed a delegated child while an
identical DAG node recovered for free; both lanes now share one predicate and a bounded
single retry. `agent.subagent_tool_call_budget` was applied only by `mission_dispatch`, so
the Settings slider silently governed half the dispatch surface; both lanes now call one
shared helper (currently `mission_dispatch.py:217-268`; `configured_subagent_tool_budget`
would sit more naturally in `tool_budget.py`, deferred).

**Layered clocks with no margin.** `ratelimit.BACKOFF_MAX_S` is 900 s and `wait()` slept it
synchronously, while the largest harness ceiling anywhere is 600 s — so a site punished
earlier in the same mission guaranteed abandonment (`research_datasheet_fetch`, 180 s, 5x
over). `wait()` now takes `max_wait_s`, and the three agent-reachable session paths
short-circuit to a `cooldown_active` teaching refusal with `retry_after_s`: **35.1 ms
instead of a 900 s sleep**. Budget 20 s, sitting strictly below `BACKOFF_BASE_S`=30 (so every
rung of the ladder short-circuits) and strictly above `DEFAULT_MIN_INTERVAL_S`=3 and every
crawl-delay (so ordinary pacing still sleeps), with a test pinning that inequality. The
reveng harvest returns partial results plus a skip count instead of losing the whole gallery,
capped at 24 items. FEA: outer 300 s == inner `run_ccx` 300 s, and the outer clock starts
first, so CalculiX's own "try a coarser mesh" was unreachable; outer is now 420 s with a
derived 300 s inner (`run_fea_adaptive` had NO `timeout_s` at all). `fetch_page`'s redirect
walk was `(10+1) x 30 = 330 s` against a 120 s ceiling; a whole-walk budget brings the worst
case to 90 s, so its ceiling stays 120.

**Test-order pollution, pre-existing.** `test_arg_reconciliation.py` drives the real MCP
ToolManager and minted live documents it never discarded, so `save_touched_docs()` at each
linear-turn terminal found the orphan and failed turns that had created no document, with
`persistence_failed` / `no_project_save_dir`. It cost three unrelated linear-turn tests and
was invisible whenever either file ran alone. An autouse fixture discards minted docs AND
restores `cad_common._TOUCHED_DOCS` — restoring the list is as load-bearing as closing the
doc, since `save_touched_docs` reads it directly. Same class as the `test_cheatsheet_examples`
leak.

**OMP fork.** Hunter's internal fork lives as UNCOMMITTED local patches in the gitignored
`scratch/omp-src` (upstream `d1239fd3` + 76 lines adding `isSqliteCorruptError` and
SQLite-corruption quarantine). Its `packages/ai` half is now vendored, but its only consumer
was `packages/coding-agent`, which CAD does not vendor — so the improvement was inert here.
CAD's own equivalent now lives in `vendor/omp/bridge/src/auth-context.ts` (OUR file, survives
re-vendor): a corrupt `omp-bridge-auth.db` is quarantined with its `-wal`/`-shm` companions
and recreated once, warning to stderr only. Verified end to end — before, every auth RPC
failed for the process lifetime with "file is not a database". `OMP_VENDOR.lock` now records
`dirty` + `dirty_files` so it stops claiming the vendored bytes are reproducible from an
upstream sha they are not, and `--verify` no longer reports its own gitignored install
artifacts as drift (it was permanently red, which trains people to ignore it).

**Deferred / known-red.** (1) `test_mega11_legs::test_ecad_engineer_role_has_domain_tools_and_delegate`
still fails, as it did at HEAD: it demands `ecad_add_power`/`board_from_schematic`/
`ecad_run_erc`/`ecad_run_drc`/`ecad_simulate`/`ecad_antenna_design`, which together with the
schematic build loop need ~22 tools. `board_from_schematic` is reachable only through
`spec_writer`'s `family: ecad`, and ERC/DRC/power/sim/antenna are held by NO role at all —
the PCB half of the ECAD pipeline is unreachable. That needs a deliberate role split
(schematic vs PCB), not more seats. (2) `test_prohibited_auth::test_no_consumer_oauth_token_reuse`
remains a deliberate open product decision. (3) The shared budget helper's home, above.

## 2026-08-27 — OMP fork: port what applied, and close the cowork-mode document gap

Audited Hunter's oh-my-pi fork (`Hunter-124/oh-my-pi`, 10 branches, checkout at
`~/Repositories/omp-inspection/oh-my-pi`) against CAD and ported what actually applies.
Its `REQUIREMENTS.md` is the binding mission statement and names CAD as a consumer;
its clauses (transparency over cleverness, failure local not machine-wide, fewer
better defaults without removing capability) drove every fix below.

**Vendored (commit `34354df3`).** `cad/oauth-fix-on-vendored-base` is cherry-picked onto
CAD's exact pin, so adopting it was a no-version-jump change. CAD genuinely serves that
page (`callback-server.ts:14` imports `oauth.html` as text; Anthropic/Codex/Google/Devin/
GitLab-Duo/Z.ai all terminate there), and its `window.close()` is a no-op for a tab the
script did not open, so the button was dead AND the promised auto-close never fired.
Also fixed `vendor_omp.py` silently deleting `vendor/omp/bun.lock` on every re-vendor —
the C++ core installs `node_modules` from that committed lock with `--frozen-lockfile`,
so re-vendoring broke provider auth at the next clean build. Now preserved like `bridge/`;
`scripts/test_vendor_omp.py` guards it (neuter-verified).
**Deliberately NOT ported:** `feat/catalog-cursor-effort-and-release-date`. Its data lives
in a 2 MB generated `models.json` and CAD's pinned copy has ZERO date fields, so porting
the predicate ships dead code. Regenerating that blob is version-jump-class work.

**Cost honesty.** 1646 of 3886 catalog models carry no price (`cursor`, `devin`,
`google-gemini-cli`, `xai-oauth`, `ollama-cloud` have zero priced models; nanogpt 539).
All were rendered as measured $0 and folded into a total whose tooltip asserted
"OMP-observed spend". `models.ts` now emits `priced` per model plus
`pricedModels`/`totalModels`/`unpricedModels[]` on `cost_summary`; both C++ aggregation
sites (`settings.cpp drawSessionCostStrip`, `ai/metrics_view.cpp` — fixing one leaves the
other lying) now show a floor with an "N of M priced" provenance, and unpriced rows read
"no price data" rather than "free". Genuinely-local runtimes keep "free" (hosted
`ollama-cloud` deliberately excluded).

**Auth truth + failure locality.** `computeAuthState`'s `state:"error"` branch was
structurally dead: it filtered for `disabledCause !== null` over rows from
`listAuthCredentials`, whose SQL is hard-coded `WHERE disabled_cause IS NULL`. A revoked
credential therefore reported `signed_out`, so users re-signed-in blindly forever, and
`listDisabledCredentials` had zero call sites. Now reachable with cause + a removable
`disabled[]`, with user-initiated causes excluded (Hunter's real db holds 4 rows all
caused "deleted by user" — without that filter his panel would have shown false errors).
Separately, a non-corrupt auth-db open failure (CANTOPEN/IOERR) cached its rejected
promise at module scope and killed every provider RPC for the bridge's whole life; the
memo is now cleared with a 2 s cooldown (strace: 12 calls → 2 `openat`).

**Settings reachability.** The canonical Generation block (reasoning effort, max tokens,
context window, with clamping) was doubly unreachable — the sole caller passed
`ProviderModel`, and that call sat inside `if (!s_omp.available)`. Reasoning effort had no
Settings control at all while the search index offered an entry that went nowhere. Now a
Generation card on Providers, search entries retargeted, dead `drawProviderModelPicker`
and the `All` enum value removed, and the degrade-path call now persists its edits (it
discarded the save-me return).

**Cowork mode** (external MCP client driving CAD). `save_touched_docs()` — the only thing
that turns an AI-created part into a real file — was wired ONLY into the two internal
turn boundaries, so an external client's work was never durably saved, and the orphan
watchdog's `os._exit(0)` skipped even atexit. `server.py` now has a bounded terminal sweep
on EOF/signal/atexit and the watchdog saves before its hard exit. `task_list` is now on the
shared registry; the design ledger moved into `lite` (the profile the installer recommends
for exactly the long external sessions that need it). `_doc()` no longer adopts a sole
engine-global document minted by another AI client (`doc_list` exposes only `origin_kind`,
no client id — that is the finest distinction the wire allows today), with legacy mode
gated unconditionally so single-client callers never regress.

**Left deliberately.** `delegate` stays OUT of the shared registry. It reads as a parity
gap, but two tests pin it as a mission-scoped pseudo-tool so `_unresolved_tool_error` can
name mission scope precisely — both written after a live incident where a fabricated cause
sent an engineer to the wrong subsystem. Real external delegation needs an external-facing
mission entry point binding a run_id + ledger + tree, which is design work, not a
registration line. Also open: composer folder-attach, `agent.model_assignment` has no GUI
consumer so Auto-mode roles never show their resolved model, and no basic/advanced
settings split yet.

**Also found en route:** a small PDF passed the composer's binary sniff, so its raw bytes
were inlined and invalid UTF-8 crashed `json::dump()` mid-send (`type_error.316`) — attached
documents now route through the existing `office_extract` RPC instead; and
`setup_mcp_clients.py --detect/--wizard` died with `NameError` on every run (two regex
globals that exist nowhere), i.e. the script the README tells users to run to register CAD
with Claude Code was broken.

Baseline unchanged: `services/agent`+`services/dag` 3809 pass / 2 pre-existing fail;
`services/mcp` 2891 pass / 1 pre-existing fail (proven at HEAD). NOTE for future runs:
this host's `TMPDIR` is `~/.cache/tmp`, which makes `AF_UNIX` paths too long and fails two
hostd tests that pass under `TMPDIR=/tmp`.

---

## 2026-08-27 — Settings progressive disclosure: 155 AI controls down to 9 by default

**Why.** Owner report: "the current setup for all ai settings is like over 1 hour if you
read everything." The OMP fork's own mission statement names this exact defect — "the
surface has grown far past useful. Fewer, better defaults; everything still reachable via
config file or an explicit 'advanced' mode. **Removing a setting must never remove a
capability.**" A grep for `showAdvanced` / `advanced mode` across the modal returned **zero
hits**: there was no progressive disclosure of any kind, so a tool-call-budget slider
carried the same visual weight as "should the assistant plan before building".

**Measured first, then cut.** A full read-only inventory of the AI tab classified **155
interactive controls: 10 EVERYDAY / 145 ADVANCED / 0 auto-derivable**, per section —
Harness 9/27, Roles 0/59, Prompts 0/8, Skills 0/6, Rules 0/16, Benchmarks 0/5, Tools 1/22,
Dev Preview 0/2. Roles being entirely advanced is the load-bearing finding: per-role model
pinning is a power surface, and the everyday "which model do I use" choice already lives on
the Providers tab. So Basic showing Harness alone is what the data supports.

**Mechanism.** New `Settings::showAdvanced` (top-level `show_advanced` in config.json,
omitted while false), surfaced as a right-aligned `Advanced: On/Off` item in the tab strip
itself (`ImGuiTabItemFlags_Trailing`) so it reads as a property of the modal, not of a page.
`kAiSubAdvanced[8]` marks which sub-sections are power surfaces; the rail filters through a
`visIdx` map so the eight section bodies keep switching on their STABLE index and needed no
edits. With one visible section the rail is suppressed entirely and the pane gets the width.
Within Harness, the all-advanced preference combos, orchestration flags, Chat-display block
and budget sliders are gated (the last via early return, since that table ends the
function); Memory & Learning early-branches to its two non-duplicate everyday controls.

**Three guarantees that hiding never removes a capability.** (1) The switch. (2) config.json
stays authoritative and is read whether or not a control is painted. (3) Settings search
still indexes all ~130 entries, and a jump that lands in a hidden section **turns disclosure
on and persists it** — which also keeps the composer's `/rule` route (Rules is advanced)
working. Plus: Tools' single everyday control ("Repair / install runtime") is re-homed onto
Harness in Basic via an extracted `drawRuntimeMaintenanceSection`, rendered in exactly one
place at a time, so a broken runtime is always fixable without discovering the switch.

**Duplicates removed** (no capability cost, both were rendered on ONE screen): the second
`settings.memoryEnabled` checkbox and the second `settings.memoryTokenBudget` slider, which
Memory & Learning repeated from the session grid directly above it. Separately,
`ai_workspace.cpp` had a hand-rolled max-tokens/context-window pair lacking the canonical
widget's clamping, tooltips and "0 = automatic" hint; it now calls
`drawAiSettingsSection(...Generation)` and threads real `busy` state instead of `false`.

**Verified by A/B pixel capture** at 1340x838, tab 2. Basic (config key genuinely absent, so
this is the shipped default): 9 controls, one screen, no scroll, no rail. Advanced: full
8-item rail, all 4 preference combos, every orchestration flag, the complete Chat-display
block and all 3 budget sliders return. `./build.sh` green; `gui_lint` theme/color/glyph/dupe
clean (the 10 `scale` findings are pre-existing in `ecad/ecad_canvas.cpp`, untouched here);
`check.py` all 9 gates pass; vendor lock verifies. Because the session grid is shared, the
AI workspace Options view simplifies from the same flag.

---

## 2026-08-27 — CAD now pins the clean private OMP backbone

**Problem.** CAD still vendored six packages from public `can1357/oh-my-pi` at detached
`d1239fd3`, then carried five uncommitted patches in a gitignored clone. Meanwhile the actual
product platform, private `Hunter-124/omp`, had moved to 18.0.8 and already contained the
universal fixes: OAuth's manual-close degrade and a better shared SQLite corruption classifier
in `pi-utils` (`SQLITE_CORRUPT*` + `SQLITE_NOTADB`), re-exported by `pi-ai`. Three remaining
patches touched `coding-agent`, which CAD never vendors. Keeping the public pin therefore
duplicated fixes, hid 614 models and 8 providers, and made the private backbone irrelevant to
the product it was built for.

**Cutover.** `vendor/omp/OMP_VENDOR.lock` now pins clean private commit `54f0e24f09b5`
from `https://github.com/Hunter-124/omp.git`: 805 content-hashed runtime files, six packages,
zero dirty files, zero local patches. `scripts/vendor_omp.py` defaults to the private URL so a
future refresh cannot silently regress CAD onto public upstream. The private repo is the
universal platform; CAD's preserved `bridge/` is explicitly a thin domain/native adapter.

**Compatibility seam.** Private OMP renamed CAD's local `isSqliteCorruptError` to the shared
`isSqliteCorruptionError`; the adapter follows the platform name. Bridge deps moved from stale
`pi-utils`/`pi-natives` 17.1.5 to workspace `pi-utils` and ABI-matching `pi-natives` 18.0.8,
with a regenerated committed `bun.lock`.

**Upgrade-only defect found by a clean install.** Bun does not reliably delete an obsolete
nested workspace package. An upgraded checkout retained
`bridge/node_modules/@oh-my-pi/pi-utils@17.1.5`, which shadowed workspace 18.0.8 and failed
with a nonexistent Handlebars dependency, while a clean checkout worked. The C++ provisioner
now removes both generated `node_modules` trees under its cross-process install lock whenever
the lock fingerprint changes, then performs the frozen install. End-to-end proof seeded the
exact stale nested 17.1.5 package and a stale stamp; the real C++ provider path deleted it,
ran frozen install, started private OMP, and rendered Providers.

**Measured result.** Full read-only surface smoke: 12/12 methods succeed; 4,500 models
(was 3,886), zero missing `priced`; 78 providers (was 70), zero missing `disabled`. Private
OMP also cuts cold bridge spawn→first ping from ~305 ms to **157 ms** and module import from
~279 ms to **154 ms**. Warm `models_list` is 5.4 ms for the larger 1.63 MB/4,500-row payload.
Providers pixel-captured at 1340×838 through rebuilt `libchudware.so`.

**Platform boundary.** Universal harness behavior moves into private OMP incrementally;
CAD retains geometry/document/daemon tools, product roles/prompts/policy, and the native C++
fast path where measured latency warrants it. A concurrent OMP session owns the standalone
platform UI, so CAD exposes and consumes stable contracts rather than duplicating that work.

---

## 2026-08-27 — First harness extraction: shake now lives once in private OMP

**Duplicate removed.** `services/agent/compaction.py` carried a 319-line Python port of
OMP's shake algorithm: recent-token protection, tool-call pairing, skill-read protection,
fenced/XML scanning, min-savings gating, mutation ordering, and placeholder accounting.
Private `pi-agent-core` already owned the canonical selector/mutator and tests. Keeping both
guaranteed drift — the two had already diverged on aggressive-tail and artifact-read policy.

**Platform contribution.** Private OMP commit `3a81ac1f7e` adds
`shakeWireMessages`: a product-neutral JSON/OpenAI-style adapter inside `pi-agent-core`.
It deep-copies input, preserves unknown fields/content blocks, pairs top-level OpenAI tool
calls, calls the existing `collectShakeRegions` and `applyShakeRegions`, and returns
JSON-safe original regions. Ten focused private-platform tests and package typecheck pass.
No standalone OMP UI file was touched.

**CAD cutover.** CAD now vendors `pi-agent-core` as its seventh private OMP package.
The bridge's `shake` RPC performs runtime JSON validation and maps the platform result to
CAD's snake-case wire shape. Python retains only two product adapters: RPC lifecycle and
image-block stripping (OMP shake deliberately selects textual context regions only).

**Honesty improvement.** The old port emitted `artifact://shake` even though no artifact
store had written that ID. The universal contract now emits `[shaken ~N tokens]` and returns
the original region to a caller that genuinely wants to persist it. A recovery link is never
invented.

**Proof.** Real bridge RPC aggressively shook a 9,750-token tool result to one placeholder,
saved ~9,734 tokens, and emitted no fake artifact URL. Existing CAD compaction/session-command
tests pass unchanged apart from reversing the obsolete fake-link assertion: 25/25.
