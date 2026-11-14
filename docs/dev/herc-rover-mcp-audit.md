# HERC rover — MCP tool-call audit (2026-11-13)

Source: the three Claude Code sessions in the HERC-Rover project's `~/.claude/projects/` folder
plus their 31 subagent transcripts — **2,996 unique chudware calls** (transcripts contain
duplicate records; raw grep counts ~4,300). Read by 10 Haiku reviewers, then the top claims
were checked against the source here. Per-group detail (CALL numbers, quoted inputs/results)
was kept with the audit session (not committed).

Discarded: an early reviewer claim that JSON-stringified args (`"ops": "[...]"`,
`"fixed": "true"`) silently returned nothing. That was an artifact of my transcript
extraction, not tool behaviour — the real calls had native args and returned results.

## Root causes of the "engine went down" episodes (diagnosed 2026-11-13)

Evidence: Claude Code's MCP logs (`%LOCALAPPDATA%\claude-cli-nodejs\Cache\*HERC-Rover\mcp-logs-chudware`),
`%APPDATA%\Chudware\logs\hostd.log`, the Windows Application event log, and live repros.

1. **The MCP server stalled, then was dropped.** FastMCP ran every *sync* tool inline on the
   event loop. One long call (import, save, `wait_job`) blocked the whole session: other calls
   queued and timed out at Claude Code's 60 s limit, and the client's `notifications/cancelled`
   could not be processed. Every one of the 38 "Connection closed" events came 2–4 s after a
   "Request timed out"; a minimal FastMCP repro hangs the same way after a cancel.
   *Fix (5652349ab):* sync tools run on a worker thread behind one gate (same serial
   semantics); a call cancelled while queued never runs, so a timed-out mutation cannot land
   later; `wait_job`/`get_job` skip the gate. Verified against the real server: it answers
   after a cancel, and a cancelled queued `new_part` never creates its part.
2. **`wait_job` outlived the client.** It accepted 900 s; Claude Code times calls out at 60 s.
   *Fix:* clamps to `MCP_TOOL_TIMEOUT` − 10 s under the stdio server, returns `still_running`.
3. **Agents ran a stale engine on their own daemon.** The bridge spawned
   `host\build-win\bin\chudware-hostd.exe` (engine from Nov 9, instance key `bd916…`) while the
   GUI used `dist\Chudware` (key `e9bfd…`), so the GUI never saw agent documents and agents
   lacked four days of engine fixes. *Fix:* the staged payload leads the Windows search order.
4. **Every cold spawn on Windows "failed".** hostd verifies the payload (~800 MB SHA-256 under
   Defender) *before* binding its pipe: 5.8–6.4 s measured. The bridge declared the rendezvous
   broken at 5 s and armed a spawn cooldown → "chudware-hostd failed to start … not retrying".
   *Fix:* 25 s bind grace / 30 s connect budget on Windows.
5. **hostd crashes were invisible.** Claude Code (libuv) runs with `SEM_NOGPFAULTERRORBOX`,
   which children inherit: a crash left no WER event, dump or log line (hostd.log shows ~20
   "starting" lines with no exit). *Fix:* hostd is spawned with `CREATE_DEFAULT_ERROR_MODE`, and
   hostd now writes `logs\hostd-crash-<pid>.txt` + `.dmp` from an unhandled-exception /
   terminate / abort handler; the next start copies the report into hostd.log.
6. **Agents were told to `pkill` the shared daemon.** Removed from the rendezvous error.
7. **Agents never saw most of the guidance.** Claude Code keeps 4096 characters of the 34 KB
   server instructions (and of each tool description). A `GEOMETRY PITFALLS` paragraph now
   sits at characters 460–1094.
8. **Oversized replies (123).** `import_part(into=…)` echoed the target's whole tree including
   the embedded base64 B-rep (428 of 455 KB); `eval_interference` shipped the GUI's overlap
   preview meshes (~290 of 628 KB). *Fix (e443bd4a7 + engine flag `include_overlap_mesh`).*

Test infra found along the way: the root `conftest.py` unset `APPDATA` on Windows, so every
hostd a test spawned exited ("no owner-private state directory") — fixed; `test_daemon_bridge`
used `SIGKILL` / `os.kill(pid, 0)` (absent / TerminateProcess on Windows) — fixed.
`test_idle_exit_under_a_parked_client_reconnects_cleanly` still fails on Windows (hostd does
not idle-exit under a parked client within 30 s); not investigated.

## Fixed earlier in this audit (f7428cbf3)

| Issue | Evidence | Fix |
|---|---|---|
| `weld_member` impossible to use | `_migrate_ref_slots` moved `params.profile` → `refs.profile` ("the engine reads geometry selectors from refs"); validation then said *both* `params.profile: required` and `refs.profile: unknown ref slot`. ≥6 failed batches; agents abandoned weldments and hand-built tubes. | `profile` excluded for `weld_member` in `_REFS_ONLY_SLOTS_TYPE_EXCLUDE` (`services/mcp/cad_doc_tools.py`). Test added. |
| `cad_guide("cylinder")` returned the ISO 1219 hydraulic guide | Hit in ≥4 transcripts while agents were trying to learn cylinder `at`/`centered` semantics. | Alias renamed to `hydraulic_cylinder` (`cad_guide.py`); `cylinder` now reaches the primitive cheatsheet. Test added. |

## Second pass (2026-11-13, evening): the open findings below

Fixed unless noted; the findings are kept below as the record of what agents hit.

- **§1 transport:** doc-id reuse after a hostd restart is refused for writes
  (`doc_id_reissued`, core_bridge); `add_features(expect_revision=N)` makes a retry a no-op
  (`revision_mismatch`); in-process `job-*` ids that died with their MCP server say so
  (`job_not_in_this_process`) instead of "unknown"; a job-registry read error is reported, not
  swallowed.
- **§3 sizes:** every reply passes a ~60 KB budget in `tool_reply` that never drops
  ok/error/revision and names the narrowing param; compact paged defaults for
  `check_interference`, `measure_part`, `describe_assembly`, `get_part(summary)`,
  `add_features`, `list_standard_parts`, `solve_mates`; `cad_guide('ops')` is a 9 KB index
  with paged sections.
- **§4 FEA:** mesh refusals are rephrased as MCP params in mm on every reply path (they said
  "native -h is metres" before — wrong); multi-solid meshes report progress per solid, and a
  fill's `sub_phase:"complete"` no longer looks like the job finishing; `stalled` needs idle
  counters *and* idle CPU. Not exposed: mesher choice / element cap (the backend has one
  graded-tet mesher and a memory-derived cap).
- **§5/§6 geometry:** cut-mode features take an optional `params.body` target and warn when a
  cut splits a body, removes nothing, or passes through a body it did not cut (contract
  section in `contracts/feature_params.md`); transform errors name the live body owner and the
  accepted form; a rejected batch names the edit that broke a downstream feature; `plane.front`
  was +Y in assembly patterns (`world_axis_from_ref`) — now −Y like everywhere else; the
  "already merged" boolean error names the feature to rebuild. Not changed: the 20 s fillet
  validation budget (deliberate, tested policy).
- **§7 assembly:** component rename works (`rename` on a k-id); pose semantics documented
  (world = R·part + at, degrees, normalised axis); touching pairs are reported as contacts.
- **§8 materials:** 13 sourced rows (AISI 4140 annealed/normalized/QT, ASTM A500 B/C round
  and shaped, Zamak 3, generic HDPE, TPU 95A, NR and neoprene rubber, 52100) plus aliases
  (gray cast iron, bronze, …); AISI/SAE/UNS/ASTM prefixes are stripped after exact matches;
  `set_material` reports `density_known`, and `measure_part` warns on fallback densities.
  MatWeb refused automated requests (HTTP 403), so every row cites a standard or datasheet.
  Density-only rows (52100, HDPE, TPU, rubbers) still need strength data.
- **§9 persistence:** the stdio server adopts the client's cwd as the project folder when none
  is configured (Claude Code starts it in the project); `new_part`'s unpersisted state is one
  warning; a part whose every feature was deleted is not autosaved over its file.
- **§10:** STEP export read-back no longer fails for validity the source lacked, and repairs
  read-tolerance growth before judging (`readback_tolerance_fixed`); PLM `"."`, in-project
  paths on Windows and checkpoint summaries are fixed; the missing `cad_guide` topics exist
  (planes, primitives, transform, regions, material, weld_member, asm_edit_component).

## Ranked findings (as found)

### 1. Transport / daemon instability under parallel subagents — ~176 hard errors
88 `Request timed out`, 50 `Server "chudware" unavailable`, 38 `Connection closed`, 43
`hostd_unavailable` envelopes. Cheap read calls (`list_parts`) timed out too, so calls were
queueing behind long mutations/imports from sibling subagents sharing one hostd.
- Timed-out **mutations sometimes landed** (`import_part`, `add_features`, `save_part` on the
  34 MB assembly), and agents re-sent them → duplicate bodies/imports (`thickums support_2…_8`).
- **Doc ids are recycled** after a hostd restart (`cad_common.py:1034` already warns):
  `doc-cpp00000002` was FRAME-100, then the FEA analysis doc, then the chassis assembly across
  sessions. An edit/close after reconnect can hit the wrong part.
- The recovery hint in `daemon_bridge.py:541` tells the agent to run `pkill chudware-hostd` —
  unsafe on a daemon shared by the GUI and other sessions (agents correctly refused).

**Suggested:** per-doc serialisation with a fast path for reads; idempotency keys or a
`revision`-conditioned write so a retried mutation is a no-op; non-recycled (or name-checked)
doc ids — reject an op when `doc_id` resolves to a different part name than last seen;
replace the pkill advice with a tool-level "restart engine" that warns about open docs.

### 2. `wait_job` outlives the MCP client timeout
`wait_job` clamps to 900 s (`cad_doc_tools.py:9845`), but Claude Code's MCP request timeout
fires first — `timeout_s` 58, 100, 300, 600, 900 all returned `Request timed out` (≥13).
After restarts `get_job` then said `unknown job_id` (jobs aren't restart-durable).
**Suggested:** clamp `wait_job` well under the client timeout (~50 s) and return
`still_running:true`; persist job records across hostd restarts.

### 3. Oversized results — 123 "exceeds maximum allowed tokens"
By tool: `check_interference` 34, `import_part` 29, `add_features` 17, `cad_guide` 11
(`ops` = 65 KB), `get_part` 10 (even `summary=true`: 115–230 KB), `measure_part` 8 (up to
800 KB on a 650-solid part), `describe_assembly` 4 (733 KB–1 MB, despite "compact narrative").
Worst: when a **mutation** result overflows, the agent can't see whether it committed.
**Suggested:** a hard result budget in the MCP envelope (e.g. 20 KB) that keeps `ok`,
`revision`, `body_count`, errors and truncates the rest with a pointer; `check_interference`
should return only failing pairs + counts by default (`limit`/`cursor`), `import_part` should
not echo the full body list, `get_part(summary=true)` needs a real size cap.

### 4. FEA mesh / long solves
Fill-stage guard failures at `mesh_size_mm` 8–12; the error says "Reduce -h to <= 5 mm" and
"Retry with --mesher hybrid / --max-elems" — CLI flags the MCP tool doesn't expose, and the
guide says CLI `-h` is in metres while the message uses mm. `mesh_fineness` is undocumented
but silently accepted. One job ran >3,600 s with phases cycling 7/7→1/7, element count going
backwards, and `sub_phase:"complete"` while `status:"running"`. No HERC FEA result was ever
produced (6 failed, 7 cancelled). **Suggested:** error remedies phrased as MCP params; expose
mesher choice; monotonic progress; status must flip when the phase completes.

### 5. Primitive & cut placement semantics (geometry surprises)
- Cylinder `at` is the **centre** with symmetric height; agents assumed base → cuts missed
  and whole batches rolled back (several groups). Cheatsheet documents it, but the guide was
  unreachable (fixed above) and the tool schema doesn't say it.
- Cuts target the **last/most recent body**; holes landed in a hinge barrel instead of the
  frame, and a cut through 5 chain plates split 5 bodies into 10 with no warning.
  **Suggested:** explicit `target_body` on cut-mode features; warn when a cut changes body
  count or removes zero volume from the intended body.
- Sketch `offset` on `plane.front` runs along −Y (plane normal) — `offset:-1385` put the body
  on the wrong side. `plane.top` is the origin mid-plane, not a top face. No `plane` guide.
- Primitives fuse by default; a later boolean then failed ("operand already merged… build with
  merge:false").
- Region `centroid` selector picked the inner core instead of a tube wall (volume 418,911 vs
  166,420 mm³); `centroid` is undocumented.

### 6. `transform` / body addressing
Three different spellings (`refs.body "$3"`, `params.body "b1"`, `params.body "$3"`) gave
contradictory errors ("ref 'body' not found" vs "transform takes no refs"); "consumed by a
later feature; target its live body owner" never names the owner. `check_interference`
`members` takes component names, k-ids and body ids interchangeably, but `unknown_member`
lists only `b1..bN`. No `transform` guide topic. **Suggested:** name the live owner in the
error; one documented body selector; add a `transform` topic.

### 7. Assembly placement — all transforms, no mates
~30 components placed with raw `at`/`rotate` and **zero `asm_mate`** in the main assembly.
`save_part` warned `placement_unverified: 55 components` (ok:true), but a full
`check_interference` to clear it overflows (see 3). A checkpoint was labelled "collision-checked"
right after a check reported 18 interfering pairs. `rotate` axis normalisation, angle unit and
`at`/`rotate` composition order are undocumented; `rename` of a component (`k56`) fails with
"no feature". `check_interference` reports touching (0.00 mm gap) as "No interference".

### 8. Materials
Registry check (`material_get`): `4140`, `AISI 4140`, `A500`, `52100`, `HDPE`, `bronze`, `gray cast
iron`, `Zamak 3`, `TPU`, `rubber` all have no engineering record; `1045`/`AISI 1045` and `4130` do.
Adding rows needs sourced property data (left open).
Unknown: `AISI 4140`, `AISI 52100`, `HDPE`, `Zamak 3`, `bronze`, `gray cast iron` — while
`4140` alone is "accepted but unresolved". `rubber`, `TPU`, `plastic`, `A500` return ok with
only a visual preset, so mass silently uses an unrelated density (A500 bodies measured with
A36's 7.85). Agents substituted 1045/A36 without telling the user. Inside `add_features`,
`set_material` reads `key`, while the top-level tool takes `material`. **Suggested:** strip the
`AISI`/`SAE` prefix in lookup; add the missing grades; make "visual-only" a warning with
`density_known:false` that `measure_part` surfaces.

### 9. Persistence noise and surprises
`new_part` returns `ok:true, persisted:false, code:no_project_save_dir` on ~141 results; nobody
acts on it. A batch that deleted every feature auto-saved the empty part over the file.
`render_part(save_path)` needs `CHUDWARE_PROJECT_SAVE_DIR`, which the Claude Code MCP config
never sets (6 failures). **Suggested:** have `register_mcp.py` set a project dir (or fall back
to the MCP client's roots/cwd); refuse to auto-save a document that went to zero features.

### 10. Smaller
- Export validity contradiction: `check_geometry_health` sound/brep_valid, but `export_part`
  re-read "1/1 solids (0 valid)"; `heal_body` no-op. Agent rebuilt the part.
- STL export "coincident facet corners" with empty `detail` (OBJ worked).
- Fillet self-intersection validation hit its 20 s budget and rejected the whole batch.
- One bad op rejects an entire batch; the error sometimes names a downstream feature
  (`f10 shell`) rather than the edited one (`f9 fillet`).
- PLM: `plm_status(".")` → `project_root_unavailable`; "item path is outside the project"
  for an in-project file under code `invalid_mode`; checkpoint summaries say
  "First version…"/"No changes." for real changes.
- `plane.front` normal is −Y for sketches (`SketchPlane::front`) but +Y in assembly mate
  resolution (`core/doc/assembly.cpp:1448`) — worth checking for sign mismatches.
- `cad_guide` has no `transform`, `plane`, `primitive`, `sphere`, `material`, `weld_member`,
  `asm_edit_component` topics (weldment cheatsheet exists but isn't reached by `weld_member`).

## Human feedback in the top-level sessions (design, not tool bugs)
"the way the chain is done right now is unacceptable", "we should probably have an axle in
between the front and the back", trim/flush the rear section and add a hand hold. These came
after the agent relied only on clash checks; no render-based self-review was done before
reporting.

## Setup note
Many of the errors in #1 come from running many subagents at once against one hostd from
Claude Code. Until #1 lands, cap concurrent modeling subagents at 2–3 and have each one
`save_part` before long imports/FEA.
