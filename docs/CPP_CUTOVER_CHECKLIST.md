# C++ Doc + Agent Cutover Checklist

**Status:** PREPARED, **NOT APPLIED**. The flag flip and the Python deletion are
left UNCOMMITTED — they are the user's call, **gated on a live model-key
validation pass** (see *Validation gate* below). This document is the only
committed deliverable; it records exactly what to change and how to verify it.

The goal: flip the host so **Documents** and the **AI agent** default to the
pure-C++ core (`libaicad.dll`) instead of the Python worker, now that the C++
doc engine is parity-complete for parts / sketch / evaluate / weldments /
assemblies. SIM already defaults to C++; ECAD stays an explicit opt-in.

---

## 1. The exact flag edits (host — already proven to compile)

Both edits were applied, the host was rebuilt with `build.bat host` (compiled
clean — `worker_client.cpp` + `settings_modal.cpp`), then **reverted** so the
working tree and the staged `dist` exe stay on the safe Python-docs default.

### a) `host/src/worker_client.cpp` — `cppDocsFlag()` default → `true`

The env var `AI_CAD_CPP_DOCS` still overrides (1=on, 0=force Python); only the
*default* (no env set) changes.

```cpp
bool cppDocsFlag() {
    const char* f = std::getenv("AI_CAD_CPP_DOCS");
    if (f && f[0]) return f[0] != '0';  // explicit override (1=on, 0=off)
-   return false;                       // default: mature Python doc engine (C++ doc gaps, above)
+   return true;                        // default ON: C++ doc engine (sketch+evaluate ported)
}
```

Also update the stale `DEFAULT OFF (2026-06-18)` comment block directly above
`cppDocsFlag()` (lines ~22–30) to describe the new default-ON reality (the
sketch + evaluate surface is now ported, so the "fail loud / stay on Python"
rationale no longer applies).

**`cppAgentFlag()` already follows docs — no edit needed.** It returns
`cppDocsFlag()` when `AI_CAD_CPP_AGENT` is unset, so flipping docs ON
automatically routes the agent to C++ too (the agent must run on the same engine
as the doc it edits). `AI_CAD_CPP_AGENT` still overrides. Confirmed in
`worker_client.cpp` `cppAgentFlag()` (`return cppDocsFlag();`).

### b) `host/src/ui/settings_modal.cpp` — Engine status row default → `true`

The Settings ▸ General ▸ Engine rows mirror the flag reads. Only the Documents
row's fallback default is hard-coded; flip it so the row reads correctly when no
env is set:

```cpp
-   const bool docsOn = flagOn("AI_CAD_CPP_DOCS", false);
+   const bool docsOn = flagOn("AI_CAD_CPP_DOCS", true);
```

The **AI agent** row already passes `docsOn` as its default
(`flagOn("AI_CAD_CPP_AGENT", docsOn)`), so it follows Documents automatically and
needs no edit. Simulation (`true`) and ECAD (`false`) defaults are unchanged.
After the flip, with no env set the rows read: Documents = **C++ core**,
Simulation = **C++ core**, AI agent = **C++ core**, ECAD = **Python worker** —
correct. (The explanatory comment at lines ~201–204 already says docs default
ON, so it becomes accurate after this edit.)

---

## 2. Re-apply the v2-agent Python deletion

> ⚠️ **Commit-role correction.** The task brief said commit `ed446f7f` *deleted*
> the v2 agent Python. It does the opposite: `ed446f7f` is the **revert** that
> **re-added** those files (`+5416 / -14`, all `A` status). The **original
> deletion** is its parent target **`ec13f5f9`** ("Migrate P55t: delete v2 agent
> Python (orchestrator/ask/pm_assist)").
>
> To re-apply the deletion, run **`git revert ed446f7f`** (≡
> `git cherry-pick ec13f5f9`). Do this only after the validation gate passes.

### Files DELETED (20 files — `D` status in `ec13f5f9`)

```
agent/ask.py
agent/orchestrator/__init__.py
agent/orchestrator/events.py
agent/orchestrator/machine.py
agent/orchestrator/micro.py
agent/orchestrator/ops_emit.py
agent/orchestrator/roles.py
agent/orchestrator/run.py
agent/orchestrator/state.py
agent/pm_assist.py
agent/test_ask.py
agent/test_micro_orchestration.py
agent/test_offline_mode.py
agent/test_orchestrator.py
agent/test_pm_assist.py
tools/cpp_agent_support_parity.py
tools/cpp_assist_parity.py
tools/cpp_micro_parity.py
tools/cpp_orch_machine_parity.py
tools/cpp_orch_run_parity.py
```

### File MODIFIED (not deleted — `M` status)

```
engine/worker.py   # strips the v2-agent RPC dispatch (agent_run_v2 / agent_ask_v2 /
                   # agent_pm_assist_v1) that the Python worker exposed; the file STAYS.
```

`engine/worker.py` is **not** removed — the deletion only edits it to drop the
now-dead v2-agent routing. Everything else the worker dispatches (docs, sim,
ecad, code-exec) remains.

---

## 3. Keep-set — Python that MUST REMAIN

These are inherent to MCP / code-exec / non-cutover subsystems and are **not**
touched by the deletion. Do not remove them:

**Code-exec / model kernel (engine):**
```
engine/kernel.py
engine/run_model.py
engine/render.py
engine/worker.py     # modified, retained (see §2)
```

**Agent runtime kept after the v2 deletion (still in `agent/`):**
```
agent/__init__.py
agent/config.py
agent/loop.py
agent/run_agent.py
agent/providers/__init__.py
agent/providers/base.py
agent/providers/anthropic_provider.py
agent/providers/openai_provider.py
agent/providers/llamacpp_provider.py
agent/providers/mock_provider.py
agent/test_compaction.py
agent/test_loop_mock.py
```

**ECAD (pinned by MCP `ecad_tools` — ECAD stays Python):**
```
engine/ecad/**          # full package — autoroute / dfm / drc / erc / impedance / formats / fab / ...
mcp_server/ecad_tools.py
mcp_server/server.py
```

`engine.ecad` is imported by `mcp_server/ecad_tools.py`, which is registered in
`mcp_server/server.py`; ECAD routing (`AI_CAD_CPP_ECAD`) stays default-OFF, so
this whole path must remain on Python.

---

## 4. Validation gate — the user MUST pass this BEFORE applying §1–§2

Launch the **already-staged** app with the C++ doc engine forced on and a real
model key configured, then exercise the full chat→model loop. The deletion
removes the Python agent fallback, so the C++ agent path must be proven live
first.

1. Configure a model key (Settings ▸ AI ▸ provider + API key), or have it in
   `%APPDATA%\AI-CAD\config.json`.
2. Launch with the doc engine forced to C++ (no rebuild needed — the env var
   overrides the current default):
   ```
   set AI_CAD_CPP_DOCS=1
   dist\AI-CAD\AI-CAD.exe
   ```
   (`AI_CAD_CPP_AGENT` is left unset so the agent follows docs → C++.)
3. Confirm **Settings ▸ General ▸ Engine** shows: Documents = C++ core,
   AI agent = C++ core, Simulation = C++ core, ECAD = Python worker.
4. Run a full agent session end-to-end and watch the log for any
   **`unknown method`** / `cpp-core error` / `cpp-agent error`:
   - chat prompt → **sketch** (enter, solve, exit)
   - **extrude** the sketch
   - **evaluate** (mass / measure / section props / interference / check)
   - build / open an **assembly** and solve mates
5. **Pass criteria:** the whole loop completes with **no `unknown method`
   errors** and no fallthrough to Python for any doc/agent RPC. Sketch mode,
   the evaluate suite, and assembly mate-solve all return real results on the
   `doc-cpp` session.

If any step fails loud, **do not apply** the flag flip or the deletion — the
C++ surface still has a gap; fix it and re-run the gate.

---

## 5. Apply order (only after the gate passes — user's call)

1. Re-apply the two flag edits in §1 (`worker_client.cpp` + `settings_modal.cpp`).
2. `git revert ed446f7f` (re-applies the §2 deletion + worker.py edit).
3. Run the full `build.bat` (not `build.bat host`) to rebuild the DLL and stage
   the new default into `dist`.
4. Smoke-test the staged `dist\AI-CAD\AI-CAD.exe` with **no** `AI_CAD_CPP_*`
   env set — the new default should now be C++ for docs + agent.
5. Commit the flip + deletion together.

---

*Prepared by the host-cutover session. Host compiles clean with the §1 edits;
working tree and staged `dist` exe were reverted to the safe Python-docs
default. Nothing in §1–§2 is committed.*
