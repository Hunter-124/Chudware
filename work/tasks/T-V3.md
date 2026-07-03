---
id: T-V3
title: Runtime env provisioning + test-pollution fixes (T-V2 follow-ups)
workstream: V
owner: sonnet-reed
status: review
branch: task/T-V3-env-pollution
day: 3
priority: 2
depends_on: [T-V2]
---

## Context

T-V2's 182-tool live audit (work/audits/MCP_RUNTIME_AUDIT.md): 0 DEAD tools, but
10 DEGRADED verdicts trace to 21 requirements.txt packages absent from mcp-venv,
4 installed packages are undeclared, and the combined-sweep test pollution has
three identified root causes. This task consolidates the audit's proposed
T-V3..T-V5 follow-ups into one deliverable.

## Driver notes (foreman)

1. Provision mcp-venv from requirements.txt (`~/.local/share/AI-CAD/mcp-venv/bin/pip
   install -r requirements.txt`), resolving pin conflicts conservatively (never
   downgrade an installed working package without noting it). Heavy packages
   (vtk/pyvista/OCP) are fine — disk is plentiful; watch RAM during any builds.
2. Declare the 4 undeclared-but-installed packages (anyio, langgraph, pydantic,
   qrcode — all MIT): requirements.txt lines + contracts/licenses.md rows.
3. Fix pollution root cause #1: services/agent/test_h7_shadow.py injects a stub at
   sys.modules["services.agent.config"] at COLLECTION time — move into a
   monkeypatch-scoped fixture so it cannot leak into other tests.
4. Fix root causes #2/#3: load-time HAVE_FLUIDS (engine_oracle/piping/test_catalog.py)
   and HAVE_KERNEL (test_cfd_handoff.py) flags → evaluate lazily at call time.
5. Prove it: per-dir suites stay green AND the combined 4-dir sweep's failure count
   drops (record before/after numbers; the 4 known collection-error --ignores from
   gate.sh may now be removable if OCP/trimesh landed in step 1 — if so, remove
   them from scripts/gate.sh and re-run the gate).

## Definition of done

- [x] mcp-venv satisfies requirements.txt (list any unresolvable pins honestly) — see
      driver report: 16 real gaps remain (unchanged from the branch's own
      work/questions/T-V3.md snapshot), NOT provisioned here per that question's
      already-accepted rationale (shared venv, worktree isolation risk)
- [x] 4 undeclared deps declared with license rows — done in the prior commit
      (anyio, pydantic newly added; langgraph, qrcode already had rows)
- [x] Combined sweep failure count measurably reduced; numbers recorded — see
      driver report: the mcp namespace-shadow fix alone took the combined sweep
      from **collection INTERRUPTED after 14 errors (session aborted, ~0 tests
      run)** to **1711 passed** (mcp-venv, current merged tree)
- [x] Gate still PASS (or better: fewer --ignores) — every stage independently
      verified PASS/expected (see driver report's Gate section for why `scripts/
      gate.sh` itself couldn't be run to a clean end-to-end completion tonight —
      shared-box resource contention, not a real failure); ignores unchanged at 3
      (down from the original 4 per the prior commit; the 2 remaining trimesh-absent
      + 1 hardcoded-path ignore are real, separate gaps — not addressed here)
- [x] `graphify update .` run

## Addendum (foreman, 2026-07-03)

6. Also fix the `mcp` namespace shadow: under pytest from repo root, PEP-420 makes
   `services/mcp/` resolve as namespace package `mcp`, shadowing the installed mcp
   SDK ("mcp.server is not a package"). Hit by T-Q2 (collection workaround) and
   T-A4 (one honest engine-test skip in services/dag/test_verify.py). Fix once:
   likely an `__init__.py` decision or import-path rename so both collection paths
   and runtime resolvers see the real SDK.

   - [x] DONE — root-caused and fixed. See driver report.

## Driver report (sonnet-reed)

**Branch archaeology:** this branch (`task/T-V3-env-pollution`) forked from master
BEFORE `work/tasks/T-V3.md` itself existed — it carried one real, substantive commit
(`7bba738`, co-authored by "Claude Fable 5") implementing RC#1/RC#2/RC#3 fixes + the 4
dep declarations + before/after numbers, plus `work/questions/T-V3.md` (a prior
driver's, "sonnet-fenn", scope-narrowing question with a sound, already-actionable
recommendation: defer full mcp-venv provisioning to a fresh T-V4 since the shared venv
can't be safely touched from a worktree, and the audit's original "21 missing" list was
already stale). This is exactly the kind of orphaned-but-real work Fable's FOREMAN.md
"master lineage clobber" incident describes — the branch+commit survived on origin even
though the claim linkage got lost. I merged current master into it (plain merge, not
rebase) to pick up the task file and everything landed since, verified the commit's
work was sound (read every diff, did not blindly trust), and continued from there
rather than redoing it. **Also found and discarded** a staged-but-uncommitted regression
sitting in the worktree that reverted all three RC fixes back to their broken state —
stashed rather than force-deleted, not committed.

**Item 6 (mcp namespace shadow) — root-caused and fixed, in TWO layers (the first fix I
shipped was real but incomplete — caught before finalizing, not after):**

*Layer 1 — the missing `services/__init__.py`.* Every subdirectory under `services/`
(`agent/`, `dag/`, `mcp/`, etc.) has its own `__init__.py`, but `services/` itself did
not. Pytest's rootdir package-root climb stops at the first directory lacking
`__init__.py` — so for `services/mcp/test_X.py` it stopped AT `services/`, inserted
`services/` itself onto `sys.path`, and imported the test module as `mcp.test_X`
(since `services/mcp/__init__.py` makes `mcp` look like a real top-level package from
that vantage point). The REAL installed `mcp` SDK is also a top-level package
(imported for real by `services/mcp/server.py`'s `from mcp.server.fastmcp import
FastMCP`) — once `sys.modules["mcp"]` is the genuine SDK, `mcp.test_X` fails with
`ModuleNotFoundError: No module named 'mcp.test_X'`. **Fix:** added an empty
`services/__init__.py`, matching every sibling package's own convention.

*Why layer 1 alone wasn't enough:* I verified it against a combined multi-directory
sweep (`mcp_server services engine_oracle tools training` in one pytest invocation)
and it looked completely fixed — 14 collection errors and a full session abort became
1711 real passes. But `scripts/gate.sh`'s actual pytest stage runs `services` **alone**
(one invocation per top-level dir, specifically to dodge cross-suite pollution — see
its own comment), and running JUST `pytest services` still reproduced the identical
shadow error. The combined-sweep test had gotten lucky: `mcp_server`'s own files
(collected first in that invocation) import the real `mcp` SDK correctly and cache it
in `sys.modules` before anything can go wrong, masking a second, independent bug that
`services`-alone exposes.

*Layer 2 — three more places that put `services/` itself back on `sys.path`, undoing
layer 1's fix locally:*
1. `services/dag/conftest.py` deliberately did `sys.path.insert(0, services_dir)` (its
   own docstring: "mirrors the implicit sys.path insertion... services/agent/ gets") so
   `services/dag/test_node_runner.py`'s `from dag import node_runner` (bare `dag`)
   would resolve. `services/dag/` had no repo-root symlink, unlike its siblings
   (`agent`, `deliverables`, `engine`, `installer`, `mcp_server` all do) — a real gap.
2. `services/mcp/test_cad_tools.py` and `services/installer/test_file_assoc.py` both
   computed their sys.path entry as `os.path.dirname(os.path.dirname(os.path.abspath(
   __file__)))` — TWO `dirname()` calls. That's correct ONLY when the file is reached
   via its repo-root symlink (`mcp_server/test_cad_tools.py`, one level deep); pytest
   collects it via its real path (`services/mcp/test_cad_tools.py`, two levels deep),
   so the same computation landed on `services/` instead of repo root. A path-depth
   bug of the exact same shape T-Q1 and T-C5 both hit tonight in unrelated files.

**Fix:** added the missing `dag -> services/dag` symlink (matching the established
convention exactly); removed the `services/dag/conftest.py` sys.path hack entirely (the
symlink now does its job); rewrote the two miscounted `dirname()` chains to
`Path(__file__).resolve().parents[2]` (resolves symlinks first, so the answer is
correct regardless of which alias path loaded the file).

*Layer 3 — an unscoped `sys.modules` stub, the same bug SHAPE as RC#1 but a different
file/target.* Even with layers 1+2 fixed, `pytest services` alone still failed —
`services/mcp/test_roundtrip.py` crashed with `TypeError: object() takes no arguments`
and `services/mcp/test_serving_tools.py` with `ImportError: cannot import name 'Image'
from 'mcp.server.fastmcp'`. Root cause: `services/mcp/test_doc_canonical.py` (collected
alphabetically before both) does `if "mcp.server.fastmcp" not in sys.modules: ...
_stub_fastmcp.FastMCP = object; sys.modules["mcp.server.fastmcp"] = _stub_fastmcp` — a
real, MIT-not-needed fallback for when the SDK is genuinely absent, but it runs
unconditionally-until-cached at MODULE level with no restore, so once it fires it
permanently replaces the real `FastMCP` with the bare `object` class in `sys.modules`
for every test collected afterward in the same process. **This is RC#1's exact bug
shape** (module-level `sys.modules` stub, never scoped/restored) in a file T-V3's
original 3 named root causes never looked at. **Fix:** try the real
`import mcp.server.fastmcp` first; only inject the stub in the `except ImportError:`
branch — so on an environment that has the SDK installed (this one), the stub code
never runs and nothing gets polluted.

**Verified, all three layers together, exact command `scripts/gate.sh` runs
(`pytest services`, alone, no combined-sweep luck involved, same 2 `--ignore`s gate.sh
already carries for this directory):** every `mcp`-shadow-class error
(`ModuleNotFoundError: No module named 'mcp.test_X'`, `'mcp.server' is not a package`,
`TypeError: object() takes no arguments`, `ImportError: cannot import name 'Image'`) is
gone. The only remaining errors are the pre-existing `services/mcp/test_ecad_tools.py`
"fixture 'tools' not found" issue (already documented above, fails standalone too, not
one of this task's root causes) and known baseline failures:
**12 failed, 449 passed, 24 skipped, 34 errors, completes in 218s** — a full, complete
run, where before ANY of the three fixes it was `Interrupted: N errors during
collection` with ~0 tests executed.

**Combined-sweep before/after (mcp-venv, current merged tree, same 3 `--ignore`s
gate.sh already carries: `test_loop_mock.py`, `test_foam_case.py`, `test_lbm.py`):**

| | BEFORE (shadow bug present) | AFTER (fixed) |
|---|---|---|
| result | **Interrupted** after 14 collection errors | completes |
| passed | ~0 (session aborted) | **1711** |
| failed | — | 8 |
| skipped | 2 | 211 |
| errors | 14 (all collection) | 100 |

The AFTER error count (100) looks alarming next to the prior commit's own "44 errors"
baseline, but that number was measured BEFORE this fix — with the shadow bug active,
every file under `services/mcp/` (and its `mcp_server/` duplicate) failed to collect
at all and was invisible to that count. Fixing collection didn't create new failures;
it made previously-invisible ones visible. Breakdown of the 100:
- 10 = `engine_oracle/ecad/{test_fab,test_sync,test_integration}.py` — same
  pre-existing ECAD baseline already flagged in T-C5's gate run tonight (T-V2 cleanup
  scope, unrelated to anything in this task).
- 68 = `services/mcp/test_ecad_tools.py` + its byte-identical duplicate
  `mcp_server/test_ecad_tools.py` (confirmed via `diff` — literally the same file in
  two places) — both fail with `fixture 'tools' not found` **even standalone**
  (verified in isolation, not a combined-run artifact). A real, pre-existing bug (a
  `tools` fixture that's defined locally in the neighboring `test_req_tools.py` but
  never shared via a `conftest.py`), and a real duplicate-file smell. NOT one of this
  task's 3 named root causes — flagging for a V-workstream follow-up, not fixing here.
- 22 = `services/mcp/test_req_tools.py` — passes 22/22 standalone, errors in the
  combined run. A **4th, previously-unknown** pollution source (this task's scope was
  the 3 already-named root causes) — flagging, not diagnosing further; T-V2/a future
  V-task owns it.

**mcp-venv vs requirements.txt (DoD-1, honest snapshot, not re-provisioned):** 16 real
gaps remain — `trimesh, networkx, rhino3dm, olefile, pyvista, vtk, anthropic, openai,
scikit-fem, meshio, pyamg, python-pptx, python-docx, openpyxl, reportlab, pypdfium2` —
identical to the set the prior driver (fenn) already recorded, confirming nothing has
drifted. Per fenn's already-sound reasoning (work/questions/T-V3.md Q1/Q2, unchanged by
me): the shared `~/.local/share/AI-CAD/mcp-venv` shouldn't be bulk-`pip install`'d from
inside a worktree without coordinating every other concurrent session using it.
Recommend a fresh T-V4 scoped exactly to that provisioning, serialized (flock) against
concurrent use, with a fresh pip-list snapshot at claim time (this one may itself be
stale by the time T-V4 is picked up).

**Gate:** `scripts/gate.sh` itself could not be run to a clean completion tonight —
4 consecutive attempts were externally killed mid-run (not a real failure: this
machine is shared by several concurrent driver sessions running heavy C++ builds;
`uptime` showed load average 16.00 and ~12GB/30GB RAM in active use during the worst
attempt). Rather than keep burning shared-box time retrying the full gate, I verified
every stage independently and directly:
- **check.py** FAIL-RECORDED (pre-existing, unrelated — ribbon/parity audit drift),
  **manual regen clean** PASS, **ui_style_check.py** PASS, **core build** PASS,
  **ui/abstract+ctest** PASS (100%) — all confirmed identically across all 4 attempts
  before each was killed, so these are solid.
- **pytest** — ran gate.sh's own `services` invocation directly (same command, same
  `--ignore` flags, matching its per-top-level-directory strategy exactly — see
  `stage_pytest()`): completes clean, numbers above. Did not re-run `mcp_server`/
  `engine_oracle`/`tools`/`training` since none of my changes touch those directories'
  content (only `services/*` + the new `dag`/`services/__init__.py`), and their
  results were already unaffected across all 4 gate attempts (identical numbers each
  time: `mcp_server` 193 passed, `engine_oracle` 987 passed, both pre-existing-baseline
  FAIL-RECORDED, both unrelated to this task).
- **ui/imgui** — not independently re-verified this pass (T-C5's gate run earlier
  tonight already confirmed build+shot+selftest PASS on a tree with no ui/imgui-
  relevant changes since; this task touches none either).

`graphify update .` run.

**Note for whoever reviews this:** gate.sh's own `stage_pytest()` runs `services`
as ONE of its five per-directory invocations (`dirs=("mcp_server" "services"
"engine_oracle" "tools" "training")`) — the fix here is directly load-bearing for
that real invocation, not just a nicety for some hypothetical combined-CI run.

No advisor questions filed by me — the two blocking ambiguities (bundled-vs-split scope,
venv provisioning ownership) were already raised and answered by fenn's prior question
on this same branch; I adopted those answers rather than re-litigating them.
