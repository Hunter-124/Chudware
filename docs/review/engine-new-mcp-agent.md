# Phase 1 Review — track: engine-new-mcp-agent

Branch: `feat/gui-rewrite` · Date: 2026-06-27
Ownership: `engine/dfm/**`, `engine/intake/**`, `mcp_server/**`, `agent/**`

Summary: **4 fixes** (2 files) · **8 flags** · **1 test added** · **3 files touched**

All fast unit tests re-run green after edits:
`engine.dfm.test_process_profiles` 42/42, `engine.intake.test_intake` 82/82,
`agent.test_loop_mock` PASS, `agent.test_compaction` PASS, and the full
`mcp_server` import chain (`cad_tools`/`gsl_tools`/`intake_tools`) imports clean.
No integration/DLL builds were run (Phase 2).

---

## Fixed

### F1. `mcp_server/cad_tools.py` — `_LAST_VOLUME` not seeded on doc creation (write-diff correctness)
The GSL Stage-1a write-diff (`_write_diff`) is *declared* to report a volume delta
(`volume 0.0 -> X (+X)`) on `add_features`, but its baseline `_LAST_VOLUME` was only
ever populated on **autosave recovery** (`recover_autosaved`, line ~352) and after a
successful `add_features` (line ~889). For a fresh `new_part`, a `.step+` open, or an
`import_file`, the baseline was `None`, so the very first op silently degraded to the
no-baseline branch (`"volume now X mm^3"`) instead of a delta — the feature did not work
on the common path it was built for.

Seeded the baseline on all three doc-creation paths (additive, no response-shape change):
- `new_part`: `_LAST_VOLUME[did] = 0.0` (an empty part has zero volume).
- `.step+` open: `_LAST_VOLUME[new_id] = r.get("volume")` when the engine reports one.
- `import_file`: `_LAST_VOLUME[new_id] = r.get("volume")` when the importer reports one.

Now the first `add_features` on a fresh/opened/imported part reports a real signed volume
delta. The existing read site (`_LAST_VOLUME.get(d)`) and `_write_diff` already handle
both `None` and numeric baselines, so this is strictly an improvement with no regression.

### F2. `engine/intake/intake.py` — nonsensical fallback in `coupon_spec` (dead sub-expression)
`coupon_spec`'s `shaft_in_bore` branch computed the bore diameter with:
```python
dia = _dim_value(feat.dims, "dia") or (feat.uncert_mm and 0) or 5.0
```
The middle term `(feat.uncert_mm and 0)` always collapses to `0` (if `uncert_mm` is
truthy) or `None`/`0` (if falsy), then falls through to `5.0` — it has **no effect** and
conflates measurement *uncertainty* with the bore *diameter*. Replaced with the exact
behavioral equivalent:
```python
dia = _dim_value(feat.dims, "dia") or 5.0
```
Verified equivalent: positive `dia` -> `dia`; `0`/`None` -> `5.0` in both forms.
`test_coupon_spec_bore` still passes (82/82).

---

## Flagged

### B1. `engine/intake/__init__.py` "ImportError" — does NOT reproduce (no code change)
The task brief lists "FIX `engine/intake/__init__.py` ImportError" as a key deliverable.
It does **not reproduce** in this environment, in either interpreter:
- System `py`: `import engine.intake` -> OK.
- Managed venv `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe`: `import engine.intake` -> OK.
- `from engine.intake import intake as ik` (the pattern `intake_tools.py` and `test_intake.py`
  use) -> OK.
- Full MCP chain `from mcp_server import intake_tools, gsl_tools` -> OK.
- `python -m engine.intake.test_intake` -> **82/82 PASS** (both interpreters).

The `__init__.py` is a correct 3-line implementation (not a stub): `from . import intake`
re-exports the submodule, and `engine.intake.intake` imports `engine.dfm.process_profiles`
cleanly with no circular import. The Phase-0 baseline did not list `engine.intake.test_intake`
among the tests it ran — likely the source of the "ImportError" assumption. **No change
made**: modifying a working file to chase a non-reproducing error risks regressions, and
broadening the public surface is out of lane (see B2).

**Optional enhancement (owner decision, additive API-surface change):** re-export the
public API from `__init__.py` so `from engine.intake import classify, build_ledger, ...`
works directly, instead of forcing `from engine.intake import intake as ik; ik.classify(...)`.
Not done here because listing ~28 public names risks breaking the import if any is
misspelled/renamed; recommend doing it with test coverage.

### B2. Stale hard-coded fit anchors in planner prompts (cross-track / architecture)
`engine.dfm.process_profiles` was explicitly built to replace the hard-coded prose anchors
in the planner prompt — its module docstring says so, and calls the old `press ~0.05`
anchor "unprintable — that was a real bug". Those stale anchors are **still present**:
- `agent/prompts/planner.md` line 20: `"slip fit 0.1-0.3 radial clearance, press fit ~0.05 interference"`
- `agent/loop.py` `PLAN_PROMPT` lines 292-293: `"slip fit = 0.1-0.3 radial clearance, press fit = ~0.05 interference"`

The new FDM numbers are slip **0.40** / press **0.10** (`process_profiles.PROFILES["fdm"].fit_classes_mm`),
so the prompts now contradict the DfM module. **Not fixed** because doing it properly
requires wiring `process_profiles.resolve_process` + `process_block_text` into the
in-app planner prompt — and the in-app agent loop (`agent/loop.py`) and the C++ orchestrator
(`core/agent/**`, out of this track's lane) do **not** currently call `process_profiles`
at all; only the MCP path (`mcp_server/intake_tools.py`) uses it. Removing the anchors
without wiring the replacement would leave the planner with no fit guidance. Recommend:
wire the PROCESS BLOCK into the planner prompt (architecture change), then delete the
stale anchor lines.

### B3. `engine/dfm/process_profiles.py` — `_strong_tie` dead swap + non-obvious stable-sort reliance
`_strong_tie` (lines ~419-437) sorts twice then has a swap that is **unreachable**:
```python
strong = sorted(..., key=lambda kv: (kv[1], kv[0] != "cnc_mill"), reverse=False)
strong.sort(key=lambda kv: kv[1], reverse=True)
...
if abs(top[1] - second[1]) < 1e-9 and second[0] == "cnc_mill":
    top, second = second, top   # dead: cnc_mill is already first on a tie via stable sort
```
The "prefer cnc_mill as leader on a score tie" outcome is actually achieved by the first
sort's key being preserved through the second (stable) sort; the `if/swap` can never fire
(cnc_mill is `strong[0]` on a tie, so `second` is never cnc_mill when scores are tied).
Behavior is correct and `test_steel_plate_threaded_flat_is_ambiguous` passes. Flagged
(not fixed) because it's load-bearing process-resolution logic: recommend collapsing to
one explicit sort, e.g. `key=lambda kv: (-kv[1], 0 if kv[0]=="cnc_mill" else 1)`, with
the existing ambiguity tests re-run.

### B4. `mcp_server/intake_tools.py` — `dfm_check` false-positive when engine checks unavailable
`dfm_check` wraps each check (`eval_thickness`, `eval_draft`) in its own `try/except: pass`.
If the engine doesn't expose either call, both swallow silently, `issues` stays empty, and
`ok = all(i.get("ok", True) for i in issues)` returns `all([])` = **True** — so the tool
reports `dfm_pass: True` with an empty `checks` list, looking like a clean pass when in
fact nothing was checked. Recommend reporting a `checks_run` count (or setting
`dfm_pass = None` / a "no checks executed" note) when `issues` is empty due to swallowed
errors. Not fixed: the "no checks available" semantics are a product decision.

### B5. `mcp_server/intake_tools.py` — `set_process` material key mixing (cosmetic)
`set_process` does `prof.setdefault("material", {})["default"] = material`. But `get_profile`
already populates `prof["material"]` with the process default (`{"name":"PLA","density_g_cm3":1.24}`),
so the line adds a `"default"` key *into* the default-material object, yielding e.g.
`{"name":"PLA","density_g_cm3":1.24,"default":"abs"}`. No consumer reads `material.default`
today (process resolution reads `process`, `process_block_text` reads `material.name`), so
it's harmless, but it conflates the user's chosen material with the process default. Recommend
a separate `user_material` field. Not fixed (response-shape/product decision).

### B6. `mcp_server/cad_tools.py` — `_LAST_VOLUME` never pruned (bounded memory leak)
`_LAST_VOLUME` is a module-level dict keyed by `doc_id`, seeded on creation/open/recovery
and updated after each `add_features`. It is never cleared when a doc closes. Bounded by the
number of doc ids allocated in a session (the per-process counter doesn't reset), so low
severity, but worth an explicit cleanup hook on `close_part`/`doc_close` for long-lived
servers. Flagged for a follow-up; not fixed (no `close_part` handler is in the reviewed set
and adding one is broader than this review's scope).

### B7. `mcp_server/intake_tools.py` — `submit_part_photos` reaches into a private helper
`submit_part_photos` picks the worst per-feature tier via `min(fv["tiers"], key=ik._tier_index)`
— using `engine.intake.intake`'s private (`_`-prefixed) `_tier_index`. It works, but reaching
into a private API is brittle if `intake.py` refactors that helper. Recommend exposing a
public `worst_tier(tiers)` helper in `engine.intake.intake` and using it here. Flagged.

### B8. `engine/intake/intake.py` — `coupon_spec` 3-hole bolt pattern is right-shifted (cosmetic)
The bolt-pattern coupon lays out `count` holes as `[-half + i*spacing/(count-1)]`. For
`count=3` this gives `[-s/2, +s/2, +3s/2]` (right-shifted) rather than the symmetric
`[-s, 0, +s]`. Spacing is correct (which is what the coupon verifies), so fit verification
is unaffected; the layout just isn't centered on the coupon plate. Noting for awareness;
not fixed (geometry/product choice).

---

## Tests added

- **`mcp_server/test_cad_tools.py`** — `test_new_part_seeds_volume_baseline_for_write_diff`
  (DLL-gated via the file's existing `pytestmark` skip; no CMake change). Asserts that
  `new_part` seeds `cad_tools._LAST_VOLUME[doc] == 0.0`, that the first `add_features`
  reports `changes.volume_delta_mm3 > 0` (i.e. the write-diff used the baseline rather
  than the no-baseline "volume now X" branch), and that the baseline is updated to the
  post-op volume. This directly covers fix F1.

  > Run in Phase 2 with the DLL staged: `python -m pytest mcp_server/test_cad_tools.py -v`
  > (or `python mcp_server/test_cad_tools.py`). Skips cleanly when the DLL is absent.

No CMakeLists edits. The existing `engine/intake/test_intake.py::test_coupon_spec_bore`
already covers fix F2 (passes 82/82).

---

## Files touched

| File | Change |
|------|--------|
| `mcp_server/cad_tools.py` | F1: seed `_LAST_VOLUME` in `new_part` (0.0), `.step+` open, and `import_file` (3 edits) |
| `mcp_server/test_cad_tools.py` | added `test_new_part_seeds_volume_baseline_for_write_diff` |
| `engine/intake/intake.py` | F2: removed nonsensical `(feat.uncert_mm and 0)` fallback in `coupon_spec` |

## Files reviewed (no change required)

`engine/dfm/process_profiles.py`, `engine/dfm/__init__.py`,
`engine/dfm/test_process_profiles.py` (42/42),
`engine/intake/__init__.py` (B1), `engine/intake/test_intake.py` (82/82),
`mcp_server/server.py`, `mcp_server/gsl_tools.py`, `mcp_server/intake_tools.py` (B4/B5/B7),
`agent/loop.py` (B2), `agent/config.py`, `agent/run_agent.py`,
`agent/test_loop_mock.py` (PASS), `agent/test_compaction.py` (PASS),
`agent/providers/base.py`, `agent/providers/mock_provider.py`, `agent/__init__.py`,
`agent/prompts/intake.md`, `agent/prompts/planner.md` (B2).
