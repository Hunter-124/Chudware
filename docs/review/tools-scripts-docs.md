# Phase 1 Review — tools-scripts-docs track

Branch: `feat/gui-rewrite`  ·  Captured: 2026-06-27
Track ownership: `scripts/**`, `installer/**`, `tools/**`, `build.bat`, `mcp_setup.bat`,
`config.example.json`, `.mcp.json`, `README.md`, `ARCHITECTURE.md`, `docs/**` (excl. `docs/review/**`, `docs/contracts/**`).

> **Transparency note:** no in-session code edits were made. The review was halted
> mid-investigation per orchestrator instruction ("STOP further review work immediately…
> Do not make any more code edits. Just write the report"). The `M` markers on
> `ARCHITECTURE.md` and `scripts/check.py` in the pre-session git status are from prior
> work, **not** this review. Accordingly the **Fixed** section is empty and every finding
> is recorded under **Flagged** so a follow-up pass can triage safely. The baseline
> (`docs/review/_baseline.md`) already records that all three `scripts/check.py` gates
> fail pre-existing; this report adds the root-cause analysis and the doc/tool staleness
> the FOCUS asked for (notably the missing `tools/cpp_parity.py` family).

## Fixed

None this session (investigation halted before edits; no edits made).

Safe fix candidates a follow-up pass can apply directly (low risk, in-lane):
- `docs/TESTING_STANDARDS.md:71` and `docs/GUI_TOOLING.md:110` — `--rule icons --rule theme`
  is a non-functional invocation (see Flagged #5). Correcting the command to `--rule all`
  or to two separate invocations is a one-line doc fix each.
- `ARCHITECTURE.md:64` — section header `### 2. MCP server (mcp/)` → `mcp_server/`, and
  the "HTTP/SSE" description → "stdio JSON-RPC" (see Flagged #4).
- `README.md:80` — `tools/` contents list names `cpp_parity.py, cpp_session_parity.py`
  that do not exist; should list the real tools (`profile_modeling.py`, `flamegraph.py`,
  `aicad_drive.py`, `verify_*`, `probe_*`, `mission_*_verify`).
- `tools/aicad_drive.py:9` — docstring hardcodes a machine-specific
  `C:\Users\nigga\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\...` path; drop it in favor
  of the canonical `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe` used elsewhere.

## Flagged

### 1. Missing `tools/cpp_*_parity*` family referenced throughout docs  (FOCUS item)
**Risk:** high — docs cite parity harnesses as the proof of the C++ migration strangler
("24/24 geometric parity", "7/7 session parity"), but the harness scripts do not exist in
the tree. A reader/contributor cannot reproduce the claimed parity numbers, and the
"living source of truth" docs (`CPP_MIGRATION.md`, `CPP_PERF.md`, `ZERO_PYTHON_CUTOVER.md`)
reference scripts that are absent.

Confirmed missing via `Glob tools/cpp*.py` (0 files) and `Glob tools/*parity*.py` (0 files):

| referenced tool | referenced in |
|---|---|
| `tools/cpp_parity.py` | `README.md:80`, `README.md:123`, `docs/HANDOFF-datum-axis-viewport.md:158`, `docs/CPP_PERF.md:3`, `docs/ZERO_PYTHON_CUTOVER.md:116`, `docs/CPP_MIGRATION.md:57,98,104,117` |
| `tools/cpp_session_parity.py` | `README.md` (cpp_session_parity block), `docs/CPP_MIGRATION.md:143` |
| `tools/cpp_perf_bench.py` | `docs/CPP_PERF.md:3,56` |
| `tools/cpp_surf_parity.py`, `tools/cpp_validate_parity.py`, `tools/cpp_mesh_parity.py`, `tools/cpp_fea_run_parity.py`, `tools/cpp_sim_parity.py`, `tools/cpp_lbm_parity.py` | `docs/ZERO_PYTHON_CUTOVER.md:116` |
| `tools/cpp_agent_support_parity.py`, `tools/cpp_assist_parity.py`, `tools/cpp_micro_parity.py`, `tools/cpp_orch_machine_parity.py`, `tools/cpp_orch_run_parity.py` | `docs/CPP_CUTOVER_CHECKLIST.md:96-100` |
| `tools/solver_diff.py` | `core/docs/dead_code_candidates.md:70,96,249`, `core/docs/wave2_occt_plan.md:480`, `core/README.md:40`, `corpus/README.md:4,86,89` |
| `tools/render_sketches.py` | `core/docs/dead_code_candidates.md:71,250` |
| `tools/mate_diff.py`, `tools/cpp_eval_parity.py` | `docs/CPP_PERF.md:3` |

**Suggested approach:** either (a) restore the missing harness scripts from history
(they may have been deleted in the GUI-rewrite branch — check `git log --diff-filter=D`),
or (b) if they were intentionally removed when the Python engine was deleted (see #2),
update every referencing doc to stop citing them and replace the "24/24 parity" claims
with whatever currently proves parity (e.g. `core/tests/` ctest suites — the baseline
shows 33/33 ctest passing). This is a doc-vs-code integrity issue, not a safe one-line
fix, because the claims themselves may need rewording. → **flag, don't fix.**

### 2. `scripts/parity_audit.py` harvests 0 compilers — gate permanently broken
**File:line:** `scripts/parity_audit.py:43` (`FEATURES_DIR = engine/doc/features`), and
the warning at `:530`.
**Risk:** high — `engine/doc/features/` no longer exists (the Python doc modeling engine
was deleted as part of the C++ migration; `engine/worker.py:141` comment: "The Python doc
modeling engine is gone — every doc/sketch/evaluate/… RPC now runs in the C++ core
(libaicad)"). The auditor's `harvest_compilers()` finds 0 `@register("<type>")` decorators,
so every `done`/`beyond` row in `SW_PARITY_SPEC.md` that names a `compile_<type>` reports
as drift (12 drift rows per baseline). The gate cannot catch real drift because it is
always failing. `scripts/check.py:30` runs this gate, so the aggregate gate is also
permanently red.
**Suggested approach:** rework `parity_audit.py` to harvest the C++ feature surface instead
of the deleted Python one — the feature-type dispatch lives in `core/doc/document.cpp`
(`type == "box" / "cylinder" / "sm_base_flange" / "weld_member" / "boolean" / "csys" / …`,
~30 branches) and the canonical feature-type table lives in `core/doc/validate.cpp:137`
(`{"box", {{"length","width","height"}, …}}`). This is a non-trivial rework of the
auditor's harvesters and a design decision (do we audit the C++ core now, or retire the
gate since the spec is Python-era?). → **flag, don't fix.** Cross-track: touches
`core/doc/` reading, but the fix lives in `scripts/` (in-lane).

### 3. `scripts/check.py` runs the heuristic `scale` rule without a baseline
**File:line:** `scripts/check.py:30` (`("gui_lint (icons / theme / scale)", ["scripts/gui_lint.py", "--rule", "all"])`).
**Risk:** medium — `--rule all` includes the heuristic `scale` rule, but no
`--baseline scripts/gui_lint_baseline.json` is passed and the baseline file does not
exist (`Glob` returned 0). Per `docs/TESTING_STANDARDS.md` tier 0, the **hard CI gate**
is the deterministic `icons` + `theme` rules only; `scale` is a tier-0b **ratchet** meant
to be run with a baseline. Running it without a baseline floods the gate with 22 gating
`scale` findings + 273 non-gating `scale-ws` findings (per baseline), so the aggregate
gate fails on the 22 chrome findings that are acknowledged in-flight GUI-rewrite debt.
**Suggested approach:** split `scripts/check.py` into (a) the hard gate
`gui_lint.py --rule icons --rule theme` is NOT valid as written (see #5) — use two
invocations or `--rule all` minus `scale`; and (b) a separate ratchet step
`gui_lint.py --rule scale --baseline scripts/gui_lint_baseline.json` that fails only on
NEW findings. This is a gate-config decision (and touches CI wiring) → **flag, don't fix.**

### 4. `ARCHITECTURE.md` — stale `mcp/` path + "HTTP/SSE" MCP description
**File:line:** `ARCHITECTURE.md:64` (`### 2. MCP server (mcp/)`) and `:66` ("HTTP/SSE MCP
server").
**Risk:** low–medium — the actual MCP server package is `mcp_server/` (confirmed via
`.mcp.json`, `mcp_setup.bat:78`, `installer/setup_mcp_clients.py:45`
`args: ["-m", "mcp_server.server"]`, `README.md:78`). The transport is **stdio JSON-RPC**,
not HTTP/SSE. `README.md` and `ARCHITECTURE.md` disagree on the path, and `ARCHITECTURE.md`
disagrees with the actual transport. The doc also still describes the v1 Python pipeline
as current production ("the production path still routes document ops to the Python
worker") which contradicts `engine/worker.py:141` ("Python doc modeling engine is gone…
every doc/… RPC now runs in the C++ core").
**Suggested approach:** in-lane safe fix — update the section header to `mcp_server/`,
correct "HTTP/SSE" → "stdio JSON-RPC", and reconcile the "production path" sentence with
the C++ cutover. The reconciliation crosses into architecture-doc territory though, so
the substantive rewrite is **flagged**; only the path/transport literal fixes are safe.

### 5. Documented `gui_lint --rule icons --rule theme` command is non-functional
**File:line:** `docs/TESTING_STANDARDS.md:71` and `docs/GUI_TOOLING.md:110`.
**Risk:** low–medium — `scripts/gui_lint.py:257` declares `--rule` as a single-value
`choices=["all","icons","theme","scale"]` argument (no `nargs`, no `action="append"`).
Passing `--rule icons --rule theme` makes argparse take the **last** value, so only the
`theme` rule runs — the documented "hard CI gate" silently skips the `icons` rule (the
exact bug class that rendered PM OK/Cancel buttons as a magenta "?"). `scripts/check.py`
itself uses `--rule all` (correct). Verified live: `gui_lint --rule icons` → clean;
`gui_lint --rule theme` → clean.
**Suggested approach:** safe one-line doc fix in each file — change the command to
`python scripts/gui_lint.py --rule all` (matches `check.py`) or to two separate
invocations. Listed here (not Fixed) because no edits were permitted this session.

### 6. `.mcp.json` committed with machine-specific absolute paths
**File:line:** `.mcp.json:4` (`"command": "C:\\Users\\nigga\\AppData\\Roaming\\AI-CAD\\runtime\\venv\\Scripts\\python.exe"`), `:8` (`"cwd": "C:\\Users\\nigga\\Desktop\\AI-opencad"`), `:9` (`PYTHONPATH` same).
**Risk:** medium — this is a per-machine artifact that `installer/setup_mcp_clients.py`
generates for Claude Code project-scoped wiring (see `_targets()` `:77`). Committing it
bakes one developer's `nigga` user path into the repo, so any other contributor opening
this project in Claude Code gets a server entry pointing at a non-existent Python. The
file should not be in version control (it is regenerated per-machine by `mcp_setup.bat`).
**Suggested approach:** add `.mcp.json` to `.gitignore` and `git rm --cached .mcp.json`,
leaving per-machine generation to `setup_mcp_clients.py`. This is a CI/repo-config change
(out of lane per "no CI/build config changes") and modifying `.mcp.json` could break the
user's live Claude Code wiring → **flag, don't fix.** Cross-track: `.gitignore` edits
likely belong to the orchestrator.

### 7. `mcp_setup.bat :try_python` gates on `build123d` which the C++ cutover may not need
**File:line:** `mcp_setup.bat:103` (`"%~1" -c "import mcp, build123d"`).
**Risk:** low — the gate requires both `mcp` and `build123d` to accept a Python for the
MCP server. After the C++ cutover (`engine/worker.py:141`) the doc engine runs in
`libaicad.dll`, but the legacy `agent_design` path (`engine/worker.py:148` `_METHODS`)
still uses build123d, so the gate is currently still correct. If/when `agent_design` is
deleted, this gate will reject valid runtimes. Also: a venv that has `mcp` but a broken
`build123d` (a common partial-install state for the heavy OCCT wheel) will fail setup
with a misleading "no Python found" message rather than a clear dep error.
**Suggested approach:** decouple the MCP-server import check (`import mcp`) from the
build123d check, and surface a targeted error when only the heavy dep is missing.
Behavior change → **flag, don't fix.**

### 8. `installer/register_mcp.py` vs `installer/setup_mcp_clients.py` coverage mismatch
**File:line:** `installer/register_mcp.py:38-42` (targets only Claude Desktop + Claude
Code); `installer/install.ps1:42` calls `register_mcp.py`; `mcp_setup.bat:78` calls
`setup_mcp_clients.py` (5 clients).
**Risk:** low — the shipped installer registers the MCP server with **only Claude**, while
the portable `mcp_setup.bat` registers with all 5 clients (Claude Code, Cursor, VS Code,
opencode, Antigravity). `README.md:147` presents `mcp_setup.bat` as the canonical
multi-client path, so this is likely intentional (installer targets end-users on Claude).
But a user who runs `install.ps1` and then uses Cursor will think the server is broken.
**Suggested approach:** either (a) document on `install.ps1` output that it is
Claude-only and point to `mcp_setup.bat` for other clients, or (b) switch `install.ps1`
to `setup_mcp_clients.py`. Product decision → **flag, don't fix.**

### 9. `docs/CPP_PERF.md` claims to be generated by a missing tool
**File:line:** `docs/CPP_PERF.md:3` ("Generated by `tools/cpp_perf_bench.py`"), `:56`
("re-run `tools\cpp_perf_bench.py` under the venv to regenerate this file").
**Risk:** low–medium — the generator does not exist (see #1), so the perf numbers in
`CPP_PERF.md` are not reproducible and may be stale relative to the current C++ core.
The doc presents itself as a live, regenerable artifact.
**Suggested approach:** restore `cpp_perf_bench.py` or relabel the doc as a historical
snapshot. Tied to the #1 decision → **flag, don't fix.**

### 10. `docs/TESTING_STANDARDS.md` DoD item 1 references deleted `engine/features/` path
**File:line:** `docs/TESTING_STANDARDS.md:39` ("Engine compiler registered
(`engine/features/__init__.py` + `engine/features/<f>.py`)").
**Risk:** low — `engine/features/` does not exist (the compilers moved to C++,
`core/doc/document.cpp`). The Definition-of-Done checklist for a feature is now stale for
the C++ core path. Adjacent items (DoD 3 "Worker RPC exposed where the host needs it
(`engine/worker.py` `_METHODS`)" is still partially valid — `_METHODS` survives but is
small). The whole DoD is Python-era and needs a C++ counterpart.
**Suggested approach:** rewrite DoD item 1 to reference `core/doc/document.cpp` feature
dispatch + `core/tests/` ctest. Doc-standards rewrite → **flag, don't fix.**

### 11. `scripts/hooks/pre-commit` is `#!/bin/sh` on a Windows-only repo
**File:line:** `scripts/hooks/pre-commit:1`.
**Risk:** very low — Git for Windows ships a sh, so this works in practice, and the
`$APPDATA` expansion resolves in Git Bash. But the repo is otherwise fully Windows-native
(`build.bat`, `.ps1` installers, `venv\Scripts\python.exe` paths). A contributor with a
non-Git-Bash sh on PATH could get surprising behavior. Noting for completeness; no action
recommended unless the team standardizes on PowerShell hooks.

## Tests added

None. No new tests were created (no code edits this session). Per guardrails, any new
parity-audit tests for the reworked harvester (#2) would live in the track test dir and
be listed here without editing CMakeLists — deferred to the follow-up that acts on these
flags.

## Files touched

This review did not modify any code files. The only file written is this report:

- `docs/review/tools-scripts-docs.md` (this file — new)

Pre-existing `M` markers in git status on `ARCHITECTURE.md` and `scripts/check.py` are
from prior work and are **not** edits made by this review (verified: no StrReplace/Write
tool calls against either file in this session).

## Cross-track fixes needed (handed off)

- **core track:** the parity-audit rework (#2) reads `core/doc/document.cpp` and
  `core/doc/validate.cpp`. The script edit is in-lane (`scripts/`), but if the team
  decides the auditor should also walk `core/tests/` for behavioral proof, that
  coordinates with the core track.
- **engine track:** confirm `engine/worker.py` legacy `agent_design`/`run_model` build123d
  path is still intended to ship (affects #7 and #10).
- **orchestrator:** `.mcp.json` gitignore decision (#6) and the broader "is the Python
  engine really gone?" doc reconciliation across `README.md`/`ARCHITECTURE.md`/`CPP_MIGRATION.md`
  (#1, #4, #9, #10) is an architecture-doc decision, not a track-local fix.
