# build-test — build script, gate, CMake trees and audits

Scope:
- build.sh
- scripts/gate.sh
- scripts/check.py and the audits it runs
- scripts/hooks/
- core/CMakeLists.txt, host/CMakeLists.txt
- CMakePresets.json
- .github/workflows/linux-gate.yml
- corpora/

Contracts: none. Test wiring lives in core/ and host/ CMake (`add_test`) plus the services
pytest sweep; contracts/web_parity.md is enforced by the gate.

## Entry points
- `build.sh` — modes default|app|core|daemon|wasm|all|stage|clean (usage header).
  Default/app build only the GUI in `host/build-linux`; `all` builds core → app →
  daemon. The binary is `bin/chudware`; the Ninja target is `Chudware`.
  - `build_core` rebuilds each configured consumed core tree in place: `core/build-occt-linux`
    (OCCT + hostd + polymesh CLI) and `core/build-linux`. It configures the `linux-core`
    preset only when neither tree exists, and that preset's output is not read by
    gate/GUI/clients.
  - `daemon` requires a configured `core/build-occt-linux` tree.
  - `stage` prefers that tree, falls back to `core/build-linux` with a warning,
    and deploys matching engine/hostd plus optional runtimes to `services/dist/Chudware/`.
    Dist resolves first; never mix daemon and engine from different trees.
- `scripts/gate.sh` — THE gate. Its header comment is the authoritative stage list, in order:
  - check.py (fatal), manual drift (advisory)
  - core OCCT build into `core/build-linux` (Release)
  - host GUI + test-binary build, WASM build smoke
  - core ctest, private hostd smoke, web parity probe
  - Linux packaging tests and per-unit pytest sweep
  - default-on sensor-node demo (external solver legs report prerequisites)
  - headless `m1` + `copilot_slash` screenshots
  - opt-in AppImage, V&V/modeling/E2E/bench/performance stages
- `scripts/gate.sh` `skip_stage` — the SKIP policy: `env` SKIPs (no OCCT/emsdk/venv/tools)
  keep a dev box green; `CHUDWARE_GATE_STRICT=1` (set by linux-gate.yml) turns them fatal;
  `optin` SKIPs stay SKIPs.
- `scripts/gate.sh` `resolve_gate_python` — the pytest interpreter ladder: the Chudware
  appdata venv, then legacy Billet/AI-CAD venvs, then `.venv-mcp`. Pin it with
  `CHUDWARE_GATE_PYTHON`. `scripts/provision_venv.sh` creates `.venv-mcp`.
- `scripts/check.py` `GATES` — source/docs audits; the advertised-tool audit imports
  runtime catalogs and dependencies. Use Python >=3.11 with those imports available,
  not a presumed stdlib-only environment. `stage_check` uses `CHECK_PYTHON`, then
  system python3, independently of the pytest interpreter. Failures are fatal.
- `scripts/hooks/pre-commit` — optional local hook that runs check.py only
  (`git config core.hooksPath scripts/hooks`).

## How it connects
gate.sh orchestrates source audits → native/WASM builds → ctest/daemon/parity →
pytest/content/screenshot smoke. Strict mode makes environment SKIPs fatal;
opt-in SKIPs remain SKIPs. Workers edit and return checks; the integrator runs
verification once after the combined edits, never concurrently per pack.

## Debugging playbook
- Full gate: `bash scripts/gate.sh`. Stage logs go to `/tmp/chudware-gate-<key|pid>/`, or
  `GATE_TMPDIR`.
- Audits only: `<audit python> scripts/check.py` (set `CHECK_PYTHON` for the gate).
- Core: `./build.sh core`, then `ctest --test-dir core/build-linux --output-on-failure -R <regex>`.
  If testing the separately configured OCCT development tree, use `core/build-occt-linux`.
  - Memory-heavy sim tests obey the memory governor:
    `budget = min(CHUDWARE_MEM_FRAC×total, avail−CHUDWARE_HEADROOM_GB)`.
  - `tet10_thin_wall` is RUN_SERIAL.
- Host: `./build.sh app`; tests: `ctest --test-dir host/build-linux --output-on-failure`.
  Never run simultaneous builds in one tree.
- Services pytest: `<gate python> -X utf8 -m pytest services/<pkg> -q`. Run per package;
  combined runs cross-pollute imports.
- Screenshot smoke: `CHUDWARE_SHOT=m1 ./host/build-linux/bin/chudware --ui-screenshot scratch/smoke.png`.
  For a scripted launch, wrap with `xvfb-run -a` when available unless
  `CHUDWARE_SHOT_HEADED=1`; direct binary invocation does not add xvfb automatically.
- Gotcha: `tool_panel_sync` fails after a tool is added or renamed until
  `tools/gen_tool_panel_tables.py` regenerates `host/src/ui/tool_panel_tables.h`.
- Gotcha: the manual drift check regenerates `docs/manual/*.md` in memory
  (`python -m tools.manual_gen`).

## Graphify starters
- graphify query "gate.sh stages check pytest ctest core build"
- graphify explain "resolve_gate_python"
- graphify path "check.py" "gate.sh"

## Related tasks/docs
- AGENTS.md (gate semantics), .claude/skills/chudware-dev/SKILL.md (build/gate commands)
- corpora/README.md (fixture layout; `platform_parity` is ctest-consumed)
- docs/REFACTOR_LOG.md (gate history)
