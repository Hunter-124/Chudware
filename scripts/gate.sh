#!/usr/bin/env bash
# scripts/gate.sh — the Linux build + test gate for Chudware (formerly Billet / AI-CAD).
#
# Every DAG node must leave this gate green before close (see AGENTS.md swarm
# rules + the plan file). It runs, in order:
#   1. scripts/check.py        — stdlib source/docs gate (gui_lint, feature_surface_audit,
#                                 parity_audit). Always runs; FATAL on red (T-RF3).
#   2. core CMake/Ninja build  — libchudware with -DCHUDWARE_WITH_OCCT=ON + ctest. SKIPPED with a
#                                 clear reason if the system OCCT cmake config is not found
#                                 (the gate does NOT fail just because OCCT is absent on a box).
#   2b. host GUI build         — cmake -S host -B host/build-linux + ninja Chudware.
#                                 The ui/imgui / ui/abstract (AI-CAD2) app is retired;
#                                 host/ is the single cross-platform GUI target.
#   2c. WASM build smoke       — ./build.sh wasm into a scratch dir (CHUDWARE_WEB_APP override).
#                                 The web app is the same host/ tree compiled by Emscripten;
#                                 FATAL when emsdk is provisioned and the build breaks.
#   3. ctest                   — only if the core build succeeded; best-effort (SKIP if no tests).
#   3b. hostd smoke            — daemon lifecycle/race/stop/recovery (scripts/hostd_smoke.py).
#   3d. web parity probe       — contracts/web_parity.md enforced over a scratch hostd with the
#                                 Python worker bridge disabled (scripts/web_parity_probe.py).
#   4. engine + agent pytest   — only if a runtime venv / pytest is discoverable; SKIP with a
#                                 clear reason if the Python runtime is not provisioned.
#   5. host shot smoke         — CHUDWARE_SHOT=m1 headless screenshot via xvfb-run.
#   4.7 T-FIN25 sensor-node golden demo — sim/fw content smoke (scripts/demo_sensor_node.py);
#                                 Renode/OpenFOAM-gated legs SKIP honestly via marker files.
#   6. PASS/SKIP summary.
#
# SKIP semantics: OCCT-absent, emsdk-absent and venv-absent are environment SKIPs, not
# failures — the gate stays green on a box that simply hasn't provisioned those optional
# runtimes, while still running the stdlib check + whatever builds are possible.
# That leniency is wrong in CI, where an unprovisioned runner reporting PASS having built
# nothing is a false green. Set CHUDWARE_GATE_STRICT=1 (linux-gate.yml does) to turn every
# environment SKIP into a fatal failure. See skip_stage() below.
#
# check.py is FATAL as of T-RF3 (2026-07-22): the feature_surface_audit red was fixed by
# teaching the audit the sketch3d_mode.cpp handler file + the reveng_ prefix dispatcher.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Per-run temp dir for stage logs. Stage output used to go to fixed paths under
# /tmp (gate_check.out, gate_host_build.out, …), so two gate runs on one box (e.g. a
# worktree run alongside the main checkout) silently
# clobbered each other's stage output. Key the dir to CHUDWARE_INSTANCE_KEY (the daemon
# instance a run already isolates on) when set, else this gate's PID; GATE_TMPDIR
# overrides. Everything below writes under $GATE_TMP.
GATE_TMP="${GATE_TMPDIR:-/tmp/chudware-gate-${CHUDWARE_INSTANCE_KEY:-$$}}"
mkdir -p "$GATE_TMP"
echo "gate temp dir: $GATE_TMP"

CHECK_PY_FATAL=1   # 1 = check.py failure fails the gate (T-RF3: audits green since 2026-07-22)

declare -a SUMMARY
fail_fatal=0

record() { SUMMARY+=("$1"); }   # arg: "STAGE  RESULT  detail"

# ---- SKIP policy -------------------------------------------------------------
# Every SKIP in this gate goes through skip_stage, and the kind argument is
# load-bearing:
#
#   env    a capability is missing — no cmake/ninja/ctest, no system OCCT, no
#          emsdk, no service venv, no collected tests, no built binary. This is
#          the false-green hazard: a misprovisioned runner otherwise reports
#          PASS having compiled and tested nothing.
#   optin  a heavy stage that is off by default and whose CHUDWARE_RUN_* /
#          CHUDWARE_GATE_* flag was not set. Nothing is broken and nothing was
#          asked for, so this stays a SKIP even under strict — failing it would
#          demand every push build an AppImage and run the openEMS/Renode
#          benches. To exercise those in CI, set their own flags.
#
# CHUDWARE_GATE_STRICT=1 converts every env SKIP into a fatal failure. It is set
# by .github/workflows/linux-gate.yml and never by default, so a developer box
# without OCCT keeps its green gate while a CI runner that lost its toolchain
# goes red instead of green-and-empty.
#
# Usage: skip_stage <env|optin> <stage label> <summary detail> [console detail]
skip_stage() {
    local kind="$1" label="$2" detail="$3" short="${4:-$3}" padded
    printf -v padded '%-19s ' "$label"
    if [ "$kind" = "env" ] && [ "${CHUDWARE_GATE_STRICT:-0}" = "1" ]; then
        record "${padded}FAIL    $detail [strict: environment SKIP]"
        echo "  FAIL: $short"
        echo "        CHUDWARE_GATE_STRICT=1 — this runner is missing a capability the gate needs;"
        echo "        skipping here would report PASS having verified nothing."
        fail_fatal=1
        return 0
    fi
    record "${padded}SKIP    $detail"
    echo "  SKIP: $short"
    return 0
}

# ---- Runtime python discovery ------------------------------------------------
# The provisioned service venv has moved with the product's rebrands (AI-CAD -> Billet ->
# Chudware) and a repo-local .venv-mcp also exists on dev boxes. Every stage below used to
# hardcode ONLY the Chudware path, so on a box where the venv still lives under the legacy
# AI-CAD name the pytest stage reported SKIP and the gate went green without running a
# single Python test — a false green. Probe the whole ladder, newest name first, and export
# the answer once so every stage agrees on the runner.
#
# Override with CHUDWARE_GATE_PYTHON=/path/to/python to pin a specific interpreter.
GATE_PY=""
GATE_PY_LABEL=""
resolve_gate_python() {
    [ -n "$GATE_PY" ] && return 0
    local cand
    for cand in \
        "${CHUDWARE_GATE_PYTHON:-}" \
        "$HOME/.local/share/Chudware/mcp-venv/bin/python" \
        "$HOME/.local/share/Billet/mcp-venv/bin/python" \
        "$HOME/.local/share/AI-CAD/mcp-venv/bin/python" \
        "$REPO/.venv-mcp/bin/python" \
        runtime/venv/bin/python .venv/bin/python venv/bin/python
    do
        [ -n "$cand" ] || continue
        [ -x "$cand" ] || continue
        "$cand" -m pytest --version >/dev/null 2>&1 || continue
        GATE_PY="$cand"
        GATE_PY_LABEL="service venv ($cand)"
        return 0
    done
    return 1
}

# Same ladder but for stages that only need an interpreter (no pytest), falling back to
# the system python3 rather than skipping.
gate_python_or_system() {
    if resolve_gate_python; then printf '%s' "$GATE_PY"; return 0; fi
    command -v python3 2>/dev/null || true
}

# ---- Stage 1: stdlib gate ---------------------------------------------------
stage_check() {
    echo "=== [1/6] scripts/check.py (stdlib gate) ==="
    if python3 scripts/check.py > ${GATE_TMP}/check.out 2>&1; then
        record "check.py            PASS    all stdlib gates green"
        echo "  PASS"
    else
        local rc=$?
        if [ "$CHECK_PY_FATAL" -eq 1 ]; then
            record "check.py            FAIL    exit $rc (fatal — see ${GATE_TMP}/check.out)"
            fail_fatal=1
        else
            record "check.py            FAIL-RECORDED  exit $rc (pre-existing, non-fatal — see docs/REFACTOR_LOG.md)"
        fi
        echo "  FAIL (exit $rc) — ${GATE_TMP}/check.out:"
        tail -n 8 ${GATE_TMP}/check.out | sed 's/^/    /'
    fi
}

# ---- Stage 1.5: manual regen clean (advisory, T-Q4) --------------------------
# Drift check for the auto-generated docs/manual/*.md (tools/manual_gen.py): regenerates the
# manual in-memory from the ribbon inventory + cad_guide prose + contracts summary and byte-
# compares it against the committed baseline. Advisory like check.py — never sets fail_fatal —
# because manual drift means "someone added a ribbon command or a contract and forgot to rerun
# `python -m tools.manual_gen`", not a build break.
stage_manual_gen() {
    echo "=== manual regen clean (advisory) ==="
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "manual regen clean" "python3 not on PATH" "python3 not installed"
        return 0
    fi
    if python3 -m tools.manual_gen --check > ${GATE_TMP}/manual.out 2>&1; then
        record "manual regen clean  PASS    docs/manual/*.md matches a fresh regen"
        echo "  PASS"
    else
        record "manual regen clean  FAIL-RECORDED  drift or missing coverage (non-fatal — see ${GATE_TMP}/manual.out)"
        echo "  FAIL (non-fatal) — ${GATE_TMP}/manual.out:"
        tail -n 8 ${GATE_TMP}/manual.out | sed 's/^/    /'
    fi
}

# ---- Stage 1.6: UI style conformance (advisory, T-U1) ------------------------
# scripts/ui_style_check.py enforces panel-conformance rules from
# docs/UI_PATTERN_LIBRARY.md. Advisory — pre-existing findings are absorbed into
# scripts/ui_style_check_baseline.json. Now targets host/ source; ui/imgui and
# ui/abstract are retired (see docs/PORT_LEDGER.md).
stage_ui_style_check() {
    echo "=== ui_style_check.py (advisory) ==="
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "ui_style_check.py" "python3 not on PATH" "python3 not installed"
        return 0
    fi
    if python3 scripts/ui_style_check.py > ${GATE_TMP}/ui_style.out 2>&1; then
        record "ui_style_check.py   PASS    no new panel-conformance drift"
        echo "  PASS"
    else
        record "ui_style_check.py   FAIL-RECORDED  new drift (non-fatal — see ${GATE_TMP}/ui_style.out)"
        echo "  FAIL (non-fatal) — ${GATE_TMP}/ui_style.out:"
        tail -n 8 ${GATE_TMP}/ui_style.out | sed 's/^/    /'
    fi
}

# ---- OCCT detection ---------------------------------------------------------
find_occt_config() {
    # Look for the OpenCASCADE CMake config in the standard install locations.
    local cand
    for cand in \
        /usr/lib/cmake/opencascade/OpenCASCADEConfig.cmake \
        /usr/lib64/cmake/opencascade/OpenCASCADEConfig.cmake \
        /usr/local/lib/cmake/opencascade/OpenCASCADEConfig.cmake \
        /usr/local/lib64/cmake/opencascade/OpenCASCADEConfig.cmake; do
        [ -f "$cand" ] && { echo "$cand"; return 0; }
    done
    return 1
}

# ---- Stage 2: core CMake/Ninja build ----------------------------------------
stage_core_build() {
    echo "=== [2/6] core CMake/Ninja build (libchudware, CHUDWARE_WITH_OCCT=ON) ==="
    if ! command -v cmake >/dev/null 2>&1; then
        skip_stage env "core build" "cmake not on PATH" "cmake not installed"
        return 0
    fi
    if ! command -v ninja >/dev/null 2>&1; then
        skip_stage env "core build" "ninja not on PATH" "ninja not installed"
        return 0
    fi
    if ! occt_cfg="$(find_occt_config)"; then
        skip_stage env "core build" "OCCT cmake config not found (system OCCT not installed)" "system OCCT not installed (no OpenCASCADEConfig.cmake under /usr{/lib,/lib64}/cmake/opencascade)"
        return 0
    fi
    echo "  OCCT config: $occt_cfg"
    local build_dir="core/build-linux"
    # CMAKE_BUILD_TYPE=Release is REQUIRED: without it the single-config Ninja generator
    # builds -O0 (the -O3/-DNDEBUG live in CMAKE_CXX_FLAGS_RELEASE, only applied for a Release
    # build type), and the `--config Release` on the build/ctest lines below is a NO-OP on
    # single-config Ninja. Omitting it shipped an unoptimized libchudware — the FEA solver ran
    # ~16x slower (sim ctests 34s -> 2s once Release was set). See docs/REFACTOR_LOG.md.
    if ! CCACHE_DISABLE=1 cmake -S core -B "$build_dir" -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_WITH_OCCT=ON -G Ninja > ${GATE_TMP}/cfg.out 2>&1; then
        record "core build          FAIL    cmake configure failed (see ${GATE_TMP}/cfg.out)"
        echo "  FAIL: configure"; tail -n 15 ${GATE_TMP}/cfg.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    if ! CCACHE_DISABLE=1 cmake --build "$build_dir" --config Release > ${GATE_TMP}/build.out 2>&1; then
        record "core build          FAIL    cmake build failed (see ${GATE_TMP}/build.out)"
        echo "  FAIL: build"; tail -n 15 ${GATE_TMP}/build.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    record "core build          PASS    libchudware built (OCCT ON)"
    echo "  PASS"
    CORE_BUILD_OK=1
}

# ---- Stage 2b: host GUI build (ninja Chudware) --------------------------------
# ui/abstract is deleted (retired with ui/imgui in refactor(unify)). This stage
# now builds the host/ GUI — the single cross-platform GUI target.
stage_ui_abstract() {
    echo "=== [2b/6] host GUI build (ninja Chudware) ==="
    if ! command -v cmake >/dev/null 2>&1 || ! command -v ninja >/dev/null 2>&1; then
        skip_stage env "host build" "cmake or ninja not on PATH" "cmake/ninja missing"
        return 0
    fi
    local host_build="$REPO/host/build-linux"
    if ! CCACHE_DISABLE=1 cmake -S "$REPO/host" -B "$host_build" -G Ninja -DCMAKE_BUILD_TYPE=Release > ${GATE_TMP}/host_cfg.out 2>&1; then
        record "host build          FAIL    cmake configure (see ${GATE_TMP}/host_cfg.out)"
        echo "  FAIL: configure"; tail -n 15 ${GATE_TMP}/host_cfg.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    if ! ninja -C "$host_build" Chudware > ${GATE_TMP}/host_build.out 2>&1; then
        record "host build          FAIL    ninja Chudware (see ${GATE_TMP}/host_build.out)"
        echo "  FAIL: build"; tail -n 20 ${GATE_TMP}/host_build.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    record "host build          PASS    host/build-linux/bin/Chudware built"
    echo "  PASS"
    HOST_BUILD_OK=1
}

# ---- Stage 2c: WASM web-client build smoke (T-RF3) ---------------------------
# The web app is the same host/ tree compiled by Emscripten; a native-only break of the
# WASM build is a web-parity regression, so this stage is FATAL when the toolchain is
# provisioned and the build breaks. SKIPs honestly when emsdk is absent. Stages into
# $GATE_TMP/wasm-app via CHUDWARE_WEB_APP so a live web deploy (~/chudware-web/app) is
# never clobbered.
stage_wasm_smoke() {
    echo "=== [2c/6] WASM web-client build smoke ==="
    local emsdk_dir="${EMSDK:-$HOME/.emsdk}"
    if [ ! -f "$emsdk_dir/emsdk_env.sh" ]; then
        skip_stage env "wasm build" "emsdk not found at $emsdk_dir" "emsdk not provisioned ($emsdk_dir)"
        return 0
    fi
    if ! CHUDWARE_WEB_APP="${GATE_TMP}/wasm-app" ./build.sh wasm > ${GATE_TMP}/wasm.out 2>&1; then
        record "wasm build          FAIL    ./build.sh wasm (see ${GATE_TMP}/wasm.out)"
        echo "  FAIL: wasm build"; tail -n 15 ${GATE_TMP}/wasm.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    record "wasm build          PASS    Chudware.wasm bundle built + staged (scratch dir)"
    echo "  PASS"
}

# ---- Stage 3: ctest (core) -------------------------------------------------
stage_ctest() {
    echo "=== [3/6] ctest (core) ==="
    if [ "${CORE_BUILD_OK:-0}" -ne 1 ]; then
        skip_stage env "ctest" "core build did not succeed" "no core build"
        return 0
    fi
    if ! command -v ctest >/dev/null 2>&1; then
        skip_stage env "ctest" "ctest not on PATH" "ctest not installed"
        return 0
    fi
    if ! ctest --test-dir core/build-linux -C Release --output-on-failure > ${GATE_TMP}/ctest.out 2>&1; then
        record "ctest               FAIL    ctest failures (see ${GATE_TMP}/ctest.out)"
        echo "  FAIL: ctest"; tail -n 20 ${GATE_TMP}/ctest.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    local passed
    passed="$(rg -o '[0-9]+% tests passed' ${GATE_TMP}/ctest.out | head -1 || true)"
    record "ctest               PASS    ${passed:-all tests passed}"
    echo "  PASS (${passed:-ok})"
}

# ---- Stage 3b: chudware-hostd smoke ---------------------------------------------
# The shared engine-host daemon (core/hostd): spawn -> connect -> engine call ->
# idle-exit, dual-client race -> one daemon, --stop, kill -9 stale-socket
# recovery. Uses a scratch CHUDWARE_INSTANCE_KEY so a live dev/installed daemon is
# never touched. FATAL when the binary exists and the suite fails.
stage_hostd_smoke() {
    echo "=== [3b/6] hostd smoke ==="
    local hostd=""
    for cand in core/build-occt-linux/bin/chudware-hostd core/build-linux/bin/chudware-hostd; do
        [ -x "$cand" ] && { hostd="$cand"; break; }
    done
    if [ -z "$hostd" ]; then
        skip_stage env "hostd smoke" "chudware-hostd not built" "no chudware-hostd binary"
        return 0
    fi
    if python3 scripts/hostd_smoke.py "$hostd" > ${GATE_TMP}/hostd.out 2>&1; then
        record "hostd smoke         PASS    lifecycle + race + stop + recovery"
        echo "  PASS"
    else
        record "hostd smoke         FAIL    see ${GATE_TMP}/hostd.out"
        echo "  FAIL: hostd smoke"; tail -n 12 ${GATE_TMP}/hostd.out | sed 's/^/    /'
        fail_fatal=1
    fi
}

# ---- Stage 3d: web parity probe (contracts/web_parity.md, T-RF3) ------------
# Spawns a scratch chudware-hostd with the Python worker bridge disabled (exactly what the
# WASM web client faces) and asserts contracts/web_parity.md: web_required methods are
# libchudware-served, web_unsupported gaps fail loud. FATAL when a hostd binary exists —
# a new parity hole (or an unrecorded cutover) must never merge silently.
stage_web_parity() {
    echo "=== [3d/6] web parity probe ==="
    local hostd=""
    for cand in core/build-occt-linux/bin/chudware-hostd core/build-linux/bin/chudware-hostd; do
        [ -x "$cand" ] && { hostd="$cand"; break; }
    done
    if [ -z "$hostd" ]; then
        skip_stage env "web parity probe" "chudware-hostd not built" "no chudware-hostd binary"
        return 0
    fi
    if python3 scripts/web_parity_probe.py "$hostd" > ${GATE_TMP}/web_parity.out 2>&1; then
        record "web parity probe    PASS    contracts/web_parity.md holds"
        echo "  PASS"
    else
        record "web parity probe    FAIL    see ${GATE_TMP}/web_parity.out"
        echo "  FAIL: web parity"; tail -n 12 ${GATE_TMP}/web_parity.out | sed 's/^/    /'
        fail_fatal=1
    fi
}

# ---- Stage 3c: AppImage smoke (advisory, env-gated) ---------------------------
# Builds the AppImage (wheelhouse skipped for speed — provisioning isn't what this
# stage tests) and boots it headless in daemon mode: screenshot must render, exit 0,
# and the bundled chudware-hostd must idle-exit after its linger. ADVISORY: failures are
# RECORDED as WARN, never fatal (packaging tooling + xvfb are machine-dependent).
# Opt in with CHUDWARE_GATE_APPIMAGE=1. A scratch CHUDWARE_INSTANCE_KEY isolates the run
# from any live dev/installed daemon.
stage_appimage_smoke() {
    echo "=== [3c/6] AppImage smoke (advisory) ==="
    if [ "${CHUDWARE_GATE_APPIMAGE:-0}" != "1" ]; then
        skip_stage optin "appimage smoke" "set CHUDWARE_GATE_APPIMAGE=1 to build + boot the AppImage" "CHUDWARE_GATE_APPIMAGE not set"
        return 0
    fi
    if ! command -v xvfb-run >/dev/null 2>&1; then
        skip_stage env "appimage smoke" "xvfb-run not on PATH" "xvfb-run not installed"
        return 0
    fi
    local out=${GATE_TMP}/appimage
    rm -rf "$out"; mkdir -p "$out"
    if ! packaging/linux/build_appimage.sh --out "$out" --skip-wheels \
            > ${GATE_TMP}/appimage.out 2>&1; then
        record "appimage smoke      WARN    AppImage build failed (see ${GATE_TMP}/appimage.out)"
        echo "  WARN: build failed"; tail -n 10 ${GATE_TMP}/appimage.out | sed 's/^/    /'
        return 0
    fi
    local img
    img="$(ls "$out"/Chudware-*.AppImage "$out"/Chudware-*.AppImage 2>/dev/null | head -1)"
    if [ -z "$img" ]; then
        record "appimage smoke      WARN    build reported success but no .AppImage produced"
        echo "  WARN: no artifact"; return 0
    fi
    local shot=${GATE_TMP}/appimage_shot.png key="gateimg$$"
    rm -f "$shot"
    local rc=0
    CHUDWARE_ENGINE_DAEMON=1 CHUDWARE_HOSTD_LINGER_SECS=5 CHUDWARE_INSTANCE_KEY="$key" \
        xvfb-run -a "$img" --appimage-extract-and-run --ui-screenshot "$shot" \
        >> ${GATE_TMP}/appimage.out 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        record "appimage smoke      WARN    AppImage exited rc=$rc (see ${GATE_TMP}/appimage.out)"
        echo "  WARN: run rc=$rc"; tail -n 10 ${GATE_TMP}/appimage.out | sed 's/^/    /'
        return 0
    fi
    if [ ! -s "$shot" ]; then
        record "appimage smoke      WARN    screenshot not produced ($shot)"
        echo "  WARN: no screenshot"; return 0
    fi
    # Daemon hygiene: the GUI exits after the shot; its hostd (scratch key) must
    # idle-exit within linger (5 s) + grace.
    sleep 9
    local leftover
    leftover="$(pgrep -af chudware-hostd | grep -F "$key" || true)"
    if [ -z "$leftover" ]; then
        # Instance key travels via env, not argv — also check any hostd running out of
        # an AppImage extraction dir (this run's daemon path is unique to the extract).
        leftover="$(pgrep -af 'appimage.*chudware-hostd|chudware-hostd.*appimage_extracted' || true)"
    fi
    if [ -n "$leftover" ]; then
        record "appimage smoke      WARN    chudware-hostd still running after linger+grace"
        echo "  WARN: leftover daemon:"; echo "$leftover" | sed 's/^/    /'
        pkill -f 'appimage_extracted.*chudware-hostd' 2>/dev/null || true
        return 0
    fi
    record "appimage smoke      PASS    built $(basename "$img") + xvfb screenshot + daemon idle-exit"
    echo "  PASS ($(du -h "$img" | cut -f1) image, shot at $shot)"
}

# ---- Stage 4: engine + agent pytest -----------------------------------------
# Uses the provisioned service venv discovered by resolve_gate_python() (Chudware, then the
# legacy Billet/AI-CAD names, then the repo-local .venv-mcp) — those venvs have all
# service/oracle deps installed. Emits a loud SKIP when none is found so engineers know the
# gate is not running real suites.
#
# CHUDWARE_LIBCHUDWARE is set to core/build-linux/bin/libchudware.so when it exists and the
# caller has not already set it. Without a loadable .so the oracle tests deselect (or
# skip via @pytest.importorskip); the stage still runs and collects — it just won't
# exercise core-calling paths.
#
# The files below have known collection errors. They are explicitly ignored so their
# errors don't abort collection. Status as of T-V4 (2026-07-03):
#   engine_oracle/doc/test_aicm.py        — REMOVED from ignores (T-V3: OCP now installed; collects cleanly)
#   engine_oracle/sim/test_foam_case.py   — REMOVED from ignores (T-V4: trimesh now installed; 14 tests collect)
#   engine_oracle/sim/test_lbm.py         — REMOVED from ignores (T-V4: trimesh now installed; collects cleanly)
#   services/agent/test_loop_mock.py      — file DELETED in T-RF5 (legacy agent loop removed)
#
# Pre-existing test failures inside the collected suites are RECORDED (non-fatal) here,
# matching the check.py FAIL-RECORDED policy. Only a hard collection crash or a zero-
# collected result sets fail_fatal=1. The sweep runs one pytest invocation per unit —
# per top-level directory, and for `services/` one per subpackage (plus its few
# root-level test files) — to avoid the cross-suite pollution failures that occur in a
# combined run (pytest's import ordering differs per-dir vs. combined; T-V2 owns that
# audit).
stage_pytest() {
    echo "=== [4/6] engine + agent pytest ==="

    # --- locate the python runner (see resolve_gate_python above) ---
    local venv_py=""
    if resolve_gate_python; then
        venv_py="$GATE_PY"
        echo "  runner: $GATE_PY_LABEL"
    else
        skip_stage env "pytest" "no venv with pytest found; provision ~/.local/share/Chudware/mcp-venv or set CHUDWARE_GATE_PYTHON" "no venv with pytest available (service venv not provisioned)"
        return 0
    fi

    # --- wire the engine .so if not already set ---
    # PREFER the full build-occt-linux engine over the slim build-linux one: on Linux
    # CHUDWARE_WITH_NETGEN defaults OFF (core/CMakeLists.txt), so the gate's own core-build
    # stage (which passes only -DCHUDWARE_WITH_OCCT=ON) produces a *sim-less* build-linux .so
    # whose FEA/CFD dispatch (sim_run_static/sim_run_modal) is compiled out — the FEA tests
    # then fail with "unknown method: sim_run_static". The 26 MB build-occt-linux engine is
    # the one that actually ships (dist/Chudware) and the only one with the Netgen mesher, so
    # test against THAT when present. Mirrors the chudware-hostd candidate order above.
    local so_path=""
    for cand in "$REPO/core/build-occt-linux/bin/libchudware.so" "$REPO/core/build-linux/bin/libchudware.so"; do
        [ -f "$cand" ] && { so_path="$cand"; break; }
    done
    # Accept CHUDWARE_LIBCHUDWARE as alias for CHUDWARE_LIBCHUDWARE (T-G10 rename shim)
    if [ -n "${CHUDWARE_LIBCHUDWARE:-}" ] && [ -z "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        export CHUDWARE_LIBCHUDWARE="$CHUDWARE_LIBCHUDWARE"
    fi
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ] && [ -n "$so_path" ]; then
        export CHUDWARE_LIBCHUDWARE="$so_path"
        echo "  CHUDWARE_LIBCHUDWARE=$CHUDWARE_LIBCHUDWARE (auto-detected)"
    elif [ -n "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        echo "  CHUDWARE_LIBCHUDWARE=$CHUDWARE_LIBCHUDWARE (from environment)"
    else
        echo "  CHUDWARE_LIBCHUDWARE not set and no libchudware.so under core/build-{occt-,}linux/bin; core-calling tests will skip/deselect"
    fi

    # --- per-unit sweep ---
    # One invocation per unit avoids cross-suite import-order pollution
    # (combined run has ~29 extra failures; T-V2 owns the cleanup).
    #
    # `services/` is swept one level DEEPER than the other top-level dirs: it holds
    # ~370 test files across ~20 subpackages. Measured 2026-07-29: as ONE invocation
    # `pytest services` recorded 15 failures; split per-subpackage it records 13, with
    # NO loss of collected tests (6986 -> 7071 collected; a node-id diff of combined
    # vs. split collect-only showed zero ids only in the combined set). The 2 that
    # vanish are services/mcp/test_registration_catalog.py's two catalog assertions:
    # they pass alone and under `pytest services/mcp`, and fail as soon as the
    # services/agent suite is collected first (it registers hydraulic_*/ask_user tools
    # DEFAULT_TOOL_FAMILIES doesn't know) — i.e. pure registry/import-order pollution
    # that was masking any genuine new regression. The remaining 13 (browser 9,
    # services/mcp 2, sim 2) reproduce in isolation and are genuine pre-existing
    # defects, so they are what this stage should actually be reporting.
    #
    # The unit list is DERIVED at runtime (glob), never hardcoded, so a new services
    # subpackage is picked up automatically and cannot silently go unrun. The
    # coverage guard below turns any enumeration gap into a hard failure.
    local dirs=("mcp_server")
    if [ -d "services" ]; then
        local sd f
        for sd in services/*/; do
            sd="${sd%/}"
            case "${sd##*/}" in __pycache__|.*) continue ;; esac
            # Skip subpackages with no tests at all — they would only add rc=5 noise.
            if [ -n "$(find "$sd" -name '__pycache__' -prune -o \
                        \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit)" ]; then
                dirs+=("$sd")
            fi
        done
        # Test files sitting directly in services/ (test_kernel_exec_aliases,
        # test_paths_alias, test_worker_stream): one invocation each. They are NOT
        # covered by any services/<subdir> unit, and `pytest services` would drag the
        # whole tree back in, so they are passed as explicit file targets.
        for f in services/test_*.py services/*_test.py; do
            [ -f "$f" ] && dirs+=("$f")
        done
    fi
    dirs+=("tools" "training")

    # Coverage guard: every test file under services/ must be reachable from exactly
    # one unit above. If the glob enumeration ever loses a suite (renamed subpackage,
    # tests parked in a directory the glob skips), that suite would stop running while
    # the stage still reported PASS — the exact masking this split exists to remove.
    # So a mismatch is fatal, not a warning.
    if [ -d "services" ]; then
        local svc_total=0 svc_covered=0 svc_units=0 u n
        svc_total="$(find services -name '__pycache__' -prune -o \
                     \( -name 'test_*.py' -o -name '*_test.py' \) -print | wc -l)"
        for u in "${dirs[@]}"; do
            case "$u" in services/*) ;; *) continue ;; esac
            svc_units=$((svc_units + 1))
            if [ -d "$u" ]; then
                n="$(find "$u" -name '__pycache__' -prune -o \
                     \( -name 'test_*.py' -o -name '*_test.py' \) -print | wc -l)"
            else
                n=1
            fi
            svc_covered=$((svc_covered + n))
        done
        echo "  services sweep: $svc_units units covering $svc_covered/$svc_total test files"
        if [ "$svc_covered" -ne "$svc_total" ]; then
            record "pytest              FAIL    services sweep covers only $svc_covered of $svc_total test files — the per-subpackage enumeration lost a suite"
            echo "  FAIL: services sweep covers $svc_covered of $svc_total test files — enumeration gap"
            fail_fatal=1
        fi
    fi

    local total_collected=0
    local total_passed=0
    local any_hard_fail=0

    for dir in "${dirs[@]}"; do
        if [ ! -e "$dir" ]; then
            echo "  $dir: path not found, skipping"
            continue
        fi

        local out_file="${GATE_TMP}/pytest_${dir//\//_}.out"

        # Build the ignore flags (only for dirs that have the relevant files).
        local ignore_args=()
        # T-V2/T-V3 tracked ignores: collection-error files that poison the whole run.
        # engine_oracle/doc/test_aicm.py was removed (T-V3): OCP now installed, collects cleanly.
        # T-V4: foam_case and lbm ignores removed — trimesh now installed, both collect cleanly.
        # (test_loop_mock.py ignore removed in T-RF5 — the file was deleted with the legacy loop.)
        # T-Q2: test_autosave_recovery must be collected via mcp_server/ (not services/)
        # because it imports mcp_server.cad_tools; from services/ the 'mcp' name resolves
        # to the services/mcp/ package and shadows mcp.server.fastmcp (collection crash).
        [ -f "services/mcp/test_autosave_recovery.py" ] && ignore_args+=(--ignore=services/mcp/test_autosave_recovery.py)

        echo -n "  $dir ... "
        local rc=0
        "$venv_py" -m pytest "$dir" "${ignore_args[@]}" -q --tb=no >"$out_file" 2>&1 || rc=$?

        # exit 5 = no tests collected (training has none yet) — not a failure
        if [ "$rc" -eq 5 ]; then
            echo "no tests collected (OK)"
            skip_stage env "pytest/$dir" "no tests collected in $dir"
            continue
        fi

        # Parse summary line: pytest outputs "N passed, M failed, K errors in Xs"
        local summary_line
        summary_line="$(grep -E '[0-9]+ (passed|failed|error)' "$out_file" | tail -1 || true)"
        local collected
        collected="$(grep -oE '[0-9]+ (passed|failed|error|deselected|selected)' "$out_file" | \
                     awk '{sum+=$1} END{print sum+0}')"
        total_collected=$((total_collected + collected))

        local passed
        passed="$(echo "$summary_line" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo "0")"
        passed="${passed:-0}"
        total_passed=$((total_passed + passed))

        if [ "$rc" -eq 0 ]; then
            echo "PASS  ($summary_line)"
            record "pytest/$dir         PASS    $summary_line"
        elif [ "$rc" -eq 1 ]; then
            # rc=1 = tests failed (not a crash); pre-existing failures are RECORDED, not fatal
            echo "FAIL-RECORDED  ($summary_line)"
            record "pytest/$dir         FAIL-RECORDED  $summary_line (pre-existing — see ${GATE_TMP}/pytest_${dir//\//_}.out; T-V2 owns cleanup)"
        else
            # rc=2+ = collection crash or internal error → hard failure
            echo "HARD-FAIL (rc=$rc)"
            record "pytest/$dir         FAIL    collection crash rc=$rc (see $out_file)"
            any_hard_fail=1
        fi
    done

    echo "  total collected ≈ $total_collected tests across ${#dirs[@]} units"
    record "pytest              total ≈ $total_collected collected | $total_passed passed | (T-V4: foam_case+lbm+test_aicm ignores removed — trimesh+OCP now installed; T-RF5: test_loop_mock.py deleted with the legacy loop)"

    # The stage header has claimed since T-V2 that "a zero-collected result sets
    # fail_fatal=1", but the check was never actually written — so a gate run that
    # collected NOTHING (wrong cwd, a renamed services dir, a broken interpreter)
    # reported PASS. Enforce it for real: the sweep dirs are tracked in-repo, so zero
    # collected tests across all of them is always a harness break, never an
    # environment SKIP.
    if [ "$total_collected" -eq 0 ]; then
        record "pytest              FAIL    zero tests collected across ${#dirs[@]} sweep units — the suite did not run (harness/cwd break, not an environment SKIP)"
        echo "  FAIL: zero tests collected — the Python suites did not run at all"
        fail_fatal=1
    fi

    if [ "$any_hard_fail" -ne 0 ]; then
        record "pytest              FAIL    one or more dirs had a hard collection crash (rc≥2)"
        echo "  FAIL: hard collection error in at least one dir"
        fail_fatal=1
    fi
}

# ---- Stage 5: host GUI shot smoke (CHUDWARE_SHOT=m1) -------------------------
# ui/imgui (AI-CAD2) is retired. This stage runs the host GUI headless under
# xvfb and verifies it renders a screenshot. The host binary is expected to
# exist at host/build-linux/bin/Chudware (built by stage 2b above, or a prior
# cached build). Skips gracefully when the host build did not succeed or the
# binary is absent.
stage_ui_imgui() {
    echo "=== [5/6] host GUI shot smoke (CHUDWARE_SHOT=m1) ==="
    local bin="$REPO/host/build-linux/bin/chudware"
    if [ ! -x "$bin" ]; then
        if [ "${HOST_BUILD_OK:-0}" -eq 1 ]; then
            record "host shot           FAIL    binary missing at $bin after successful build"
            echo "  FAIL: missing $bin"
            fail_fatal=1
        else
            skip_stage env "host shot" "host build did not succeed or was skipped" "host binary not available"
        fi
        return 0
    fi
    local shot="${GATE_TMP}/host-shot.png"
    local libchudware="${CHUDWARE_LIBCHUDWARE:-$REPO/core/build-occt-linux/bin/libchudware.so}"
    local shot_cmd="env CHUDWARE_NO_FILE_DIALOGS=1 CHUDWARE_SHOT=m1"
    [ -f "$libchudware" ] && shot_cmd="$shot_cmd CHUDWARE_LIBCHUDWARE=$libchudware"
    shot_cmd="$shot_cmd \"$bin\" --ui-screenshot \"$shot\""
    # Always use xvfb-run when available unless CHUDWARE_SHOT_HEADED=1 is set.
    if [ "${CHUDWARE_SHOT_HEADED:-0}" != "1" ] && command -v xvfb-run >/dev/null 2>&1; then
        shot_cmd="xvfb-run -a $shot_cmd"
    fi
    if ! eval "$shot_cmd" > ${GATE_TMP}/host_shot.out 2>&1; then
        record "host shot           FAIL    --ui-screenshot smoke (see ${GATE_TMP}/host_shot.out)"
        echo "  FAIL: host shot"; tail -n 15 ${GATE_TMP}/host_shot.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    if [ -f "$shot" ]; then
        record "host shot           PASS    CHUDWARE_SHOT=m1 headless screenshot written"
        echo "  PASS ($shot)"
    else
        record "host shot           FAIL    shot PNG not written (see ${GATE_TMP}/host_shot.out)"
        echo "  FAIL: $shot not created"
        fail_fatal=1
    fi

    # Slash-autocomplete regression (composer hang on "/"): menu capture +
    # scripted Enter fill. Two PNGs — the open menu and the filled composer.
    local slash_shot="${GATE_TMP}/copilot-slash.png"
    local slash_menu="${GATE_TMP}/copilot_slash_menu.png"
    local slash_cmd="env CHUDWARE_NO_FILE_DIALOGS=1 CHUDWARE_SHOT=copilot_slash"
    [ -f "$libchudware" ] && slash_cmd="$slash_cmd CHUDWARE_LIBCHUDWARE=$libchudware"
    slash_cmd="$slash_cmd \"$bin\" --ui-screenshot \"$slash_shot\""
    if [ "${CHUDWARE_SHOT_HEADED:-0}" != "1" ] && command -v xvfb-run >/dev/null 2>&1; then
        slash_cmd="xvfb-run -a $slash_cmd"
    fi
    if ! eval "$slash_cmd" > ${GATE_TMP}/copilot_slash.out 2>&1; then
        record "slash shot          FAIL    CHUDWARE_SHOT=copilot_slash (see ${GATE_TMP}/copilot_slash.out)"
        echo "  FAIL: copilot_slash shot"; tail -n 15 ${GATE_TMP}/copilot_slash.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    if [ -f "$slash_shot" ] && [ -f "$slash_menu" ]; then
        record "slash shot          PASS    CHUDWARE_SHOT=copilot_slash menu + fill screenshots written"
        echo "  PASS ($slash_shot)"
    else
        record "slash shot          FAIL    copilot_slash PNGs not written (see ${GATE_TMP}/copilot_slash.out)"
        echo "  FAIL: copilot_slash PNGs missing"
        fail_fatal=1
    fi
}

# ---- Stage 6: model-eval bench smoke (T-H8) ---------------------------------
# Optional, env-gated SKIP like the OCCT stages: runs the product-task eval benches at
# smoke size. Default is SKIP (stays green) so the gate does not depend on the flywheel;
# set CHUDWARE_RUN_BENCH=1 to opt in, and then a bench error DOES fail the gate. The bench
# framework is stdlib-only, so it needs nothing but python3.
stage_bench() {
    echo "=== [6/6] model-eval bench smoke (T-H8) ==="
    if [ "${CHUDWARE_RUN_BENCH:-0}" != "1" ]; then
        skip_stage optin "bench (T-H8)" "set CHUDWARE_RUN_BENCH=1 to run smoke-size eval benches" "CHUDWARE_RUN_BENCH not set"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "bench (T-H8)" "python3 not on PATH" "python3 not installed"
        return 0
    fi
    if ! python3 -m tools.flywheel.bench.runner --smoke --all --no-write > ${GATE_TMP}/bench.out 2>&1; then
        record "bench (T-H8)        FAIL    a smoke bench errored (see ${GATE_TMP}/bench.out)"
        echo "  FAIL: bench smoke"; tail -n 15 ${GATE_TMP}/bench.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    record "bench (T-H8)        PASS    smoke benches ran ($(rg -c 'baselines\[' ${GATE_TMP}/bench.out 2>/dev/null || echo '?') reported)"
    echo "  PASS"; tail -n 8 ${GATE_TMP}/bench.out | sed 's/^/    /'
}

# ---- Stage 7: modeling perf-regression (tools/perf_regression.py) -----------
# Times the geometry kernel against tools/perf_baseline.json and flags median slowdowns.
# Env-gated (perf timings are machine + load sensitive, so this is NOT a hard blocker by
# default): set CHUDWARE_RUN_PERF=1 to run. A regression is RECORDED (non-fatal) unless
# CHUDWARE_PERF_FATAL=1. Needs a loadable .so — prefers the freshly built build-occt-linux
# engine (the profiler/contention RPCs live there); falls back to whatever core_bridge
# resolves. Update the baseline deliberately with `python tools/perf_regression.py --update`.
stage_perf() {
    echo "=== [7/7] modeling perf-regression ==="
    if [ "${CHUDWARE_RUN_PERF:-0}" != "1" ]; then
        skip_stage optin "perf-regression" "set CHUDWARE_RUN_PERF=1 to time the modeling kernel" "CHUDWARE_RUN_PERF not set"
        return 0
    fi
    local perf_py=""
    perf_py="$(gate_python_or_system)"
    if [ -z "$perf_py" ]; then
        skip_stage env "perf-regression" "no python for perf harness" "no python"; return 0
    fi
    # Prefer the just-built OCCT engine so the contention/per-feature diagnostics are captured.
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ] && [ -f "$REPO/core/build-occt-linux/bin/libchudware.so" ]; then
        export CHUDWARE_LIBCHUDWARE="$REPO/core/build-occt-linux/bin/libchudware.so"
    fi
    local rc=0
    "$perf_py" "$REPO/tools/perf_regression.py" --iters "${CHUDWARE_PERF_ITERS:-5}" \
        --json ${GATE_TMP}/perf.json > ${GATE_TMP}/perf.out 2>&1 || rc=$?
    tail -n 16 ${GATE_TMP}/perf.out | sed 's/^/    /'
    if [ "$rc" -eq 0 ]; then
        record "perf-regression     PASS    no median regressions vs baseline"
        echo "  PASS"
    elif [ "$rc" -eq 1 ]; then
        record "perf-regression     $([ "${CHUDWARE_PERF_FATAL:-0}" = 1 ] && echo FAIL || echo WARN)    median regression vs baseline (see ${GATE_TMP}/perf.out)"
        echo "  REGRESSION detected"
        [ "${CHUDWARE_PERF_FATAL:-0}" = 1 ] && fail_fatal=1
    else
        skip_stage env "perf-regression" "harness could not run (rc=$rc; likely no loadable .so)" "harness rc=$rc"
    fi
}

# ---- Stage 4.5: V&V benchmark smoke (T-BL01) --------------------------------
# Optional, env-gated SKIP: runs the V&V benchmark harness in smoke mode
# (pure-Python families only — no .so required for the smoke subset).
# Default is SKIP (stays green) so the gate does not depend on external deps;
# set CHUDWARE_RUN_VV=smoke (or =full) to opt in.
# - CHUDWARE_RUN_VV=smoke  → pytest scripts/vv_benchmark.py::TestSmoke  (fast, no .so)
# - CHUDWARE_RUN_VV=full   → pytest scripts/vv_benchmark.py  (all suites, needs .so + deps)
stage_vv() {
    echo "=== [4.5/6] V&V benchmark smoke (T-BL01) ==="
    if [ -z "${CHUDWARE_RUN_VV:-}" ]; then
        skip_stage optin "vv-bench (T-BL01)" "set CHUDWARE_RUN_VV=smoke or =full to run V&V harness" "CHUDWARE_RUN_VV not set"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "vv-bench (T-BL01)" "python3 not on PATH" "python3 not installed"
        return 0
    fi

    local venv_py=""
    venv_py="$(gate_python_or_system)"

    # Wire CHUDWARE_LIBCHUDWARE if not set and the .so exists (for full mode)
    local so_path="$REPO/core/build-linux/bin/libchudware.so"
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ] && [ -f "$so_path" ]; then
        export CHUDWARE_LIBCHUDWARE="$so_path"
    fi

    local pytest_args=()
    if [ "${CHUDWARE_RUN_VV}" = "full" ]; then
        pytest_args=("scripts/vv_benchmark.py")
        echo "  mode: full (all V&V suites)"
    else
        pytest_args=("scripts/vv_benchmark.py::TestSmoke")
        echo "  mode: smoke (pure-Python subset)"
    fi

    export CHUDWARE_RUN_VV
    if PYTHONPATH="$REPO:${PYTHONPATH:-}" "$venv_py" -m pytest \
            "${pytest_args[@]}" -q --tb=short > ${GATE_TMP}/vv.out 2>&1; then
        local summary_line
        summary_line="$(grep -E '[0-9]+ passed' ${GATE_TMP}/vv.out | tail -1 || echo 'ok')"
        record "vv-bench (T-BL01)   PASS    $summary_line"
        echo "  PASS ($summary_line)"
    else
        local rc=$?
        if [ "$rc" -eq 5 ]; then
            skip_stage env "vv-bench (T-BL01)" "no tests collected (check venv + deps)" "no tests collected"
        else
            record "vv-bench (T-BL01)   FAIL    pytest exit $rc (see ${GATE_TMP}/vv.out)"
            echo "  FAIL: V&V suite (see ${GATE_TMP}/vv.out):"
            tail -n 15 ${GATE_TMP}/vv.out | sed 's/^/    /'
            fail_fatal=1
        fi
    fi
}

# ---- Stage 4.6: Modeling-op combination battery (T-BL19) --------------------
# Optional, env-gated SKIP: drives the C++ feature engine through every registered
# modeling op + a pairwise/sequence matrix and asserts on live geometry. Default is
# SKIP (stays green) so the normal gate never depends on the OCCT .so; opt in with:
# - CHUDWARE_RUN_MODELING=smoke → pytest ...::TestSmoke  (registry integrity, no .so)
# - CHUDWARE_RUN_MODELING=full  → full live battery       (needs OCCT libchudware.so)
# NOTE: full mode needs the OCCT geometry kernel (core/build-occt-linux/bin/libchudware.so);
# the plain build-linux .so has no geometry and would ALL-SKIP (which the battery fails).
stage_modeling() {
    echo "=== [4.6/6] Modeling-op combination battery (T-BL19) ==="
    if [ -z "${CHUDWARE_RUN_MODELING:-}" ]; then
        skip_stage optin "modeling (T-BL19)" "set CHUDWARE_RUN_MODELING=smoke or =full" "CHUDWARE_RUN_MODELING not set"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "modeling (T-BL19)" "python3 not on PATH" "python3 not installed"
        return 0
    fi

    local venv_py=""
    venv_py="$(gate_python_or_system)"

    # Full mode needs the OCCT geometry .so; prefer it, else fall back to any built .so.
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        for cand in "$REPO/core/build-occt-linux/bin/libchudware.so" \
                    "$REPO/core/build-linux/bin/libchudware.so"; do
            [ -f "$cand" ] && export CHUDWARE_LIBCHUDWARE="$cand" && break
        done
    fi

    local pytest_args=()
    if [ "${CHUDWARE_RUN_MODELING}" = "full" ]; then
        pytest_args=("scripts/modeling_battery.py")
        echo "  mode: full (live geometry battery; .so=${CHUDWARE_LIBCHUDWARE:-<none>})"
    else
        pytest_args=("scripts/modeling_battery.py::TestSmoke")
        echo "  mode: smoke (registry integrity, no .so)"
    fi

    if PYTHONPATH="$REPO:${PYTHONPATH:-}" "$venv_py" -m pytest \
            "${pytest_args[@]}" -q --tb=short > ${GATE_TMP}/modeling.out 2>&1; then
        local summary_line
        summary_line="$(grep -E '[0-9]+ passed' ${GATE_TMP}/modeling.out | tail -1 || echo 'ok')"
        record "modeling (T-BL19)   PASS    $summary_line"
        echo "  PASS ($summary_line)"
    else
        local rc=$?
        if [ "$rc" -eq 5 ]; then
            skip_stage env "modeling (T-BL19)" "no tests collected (check venv + deps)" "no tests collected"
        else
            record "modeling (T-BL19)   FAIL    pytest exit $rc (see ${GATE_TMP}/modeling.out)"
            echo "  FAIL: modeling battery (see ${GATE_TMP}/modeling.out):"
            tail -n 15 ${GATE_TMP}/modeling.out | sed 's/^/    /'
            fail_fatal=1
        fi
    fi
}

# ---- Stage 4.7: T-FIN25 sensor-node golden demo (sim/fw content smoke) ------
# Default ON (bounded <5 min). Legs that need Renode/OpenFOAM write SKIP markers
# when those binaries are absent — never fake a pass. Opt out with CHUDWARE_SKIP_FIN25=1.
# Fast dry thermal: CHUDWARE_FIN25_THERMAL_DRY=1. Artifacts under $GATE_TMP/fin25/.
stage_fin_content() {
    echo "=== [4.7/6] T-FIN25 sensor-node golden demo ==="
    if [ "${CHUDWARE_SKIP_FIN25:-}" = "1" ]; then
        skip_stage optin "fin25 golden demo" "CHUDWARE_SKIP_FIN25=1" "CHUDWARE_SKIP_FIN25=1"
        return 0
    fi
    if [ ! -f "$REPO/scripts/demo_sensor_node.py" ]; then
        skip_stage env "fin25 golden demo" "scripts/demo_sensor_node.py missing" "demo script missing"
        return 0
    fi

    local venv_py=""
    venv_py="$(gate_python_or_system)"
    if [ -z "$venv_py" ]; then
        skip_stage env "fin25 golden demo" "no python3" "python3 not installed"
        return 0
    fi

    # Pin a sim-enabled engine when present (motion/thermal FEA); never invent one.
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        for cand in "$REPO/core/build-occt-linux/bin/libchudware.so" \
                    "$REPO/core/build-linux/bin/libchudware.so"; do
            [ -f "$cand" ] && export CHUDWARE_LIBCHUDWARE="$cand" && break
        done
    fi

    local out="${GATE_TMP}/fin25"
    mkdir -p "$out"
    export CHUDWARE_FIN25_OUT="$out"
    export CHUDWARE_FIN25_MARKERS="$out/markers"
    export CHUDWARE_PROJECT_DIR="${CHUDWARE_PROJECT_DIR:-$out/project}"

    local rc=0
    if ! PYTHONPATH="$REPO:${PYTHONPATH:-}" \
            "$venv_py" "$REPO/scripts/demo_sensor_node.py" \
            > "${GATE_TMP}/fin25.out" 2>&1; then
        rc=$?
    fi

    # Summarise per-leg markers (PASS/FAIL/SKIP) for the gate line.
    local n_pass=0 n_fail=0 n_skip=0
    if [ -d "$out/markers" ]; then
        n_pass=$(grep -l '^PASS' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
        n_fail=$(grep -l '^FAIL' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
        n_skip=$(grep -l '^SKIP' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
    fi
    local detail="pass=$n_pass fail=$n_fail skip=$n_skip (see ${GATE_TMP}/fin25.out)"

    if [ "$rc" -eq 0 ] && [ "$n_fail" = "0" ]; then
        record "fin25 golden demo   PASS    $detail"
        echo "  PASS ($detail)"
    else
        record "fin25 golden demo   FAIL    exit=$rc $detail"
        echo "  FAIL: fin25 golden demo (see ${GATE_TMP}/fin25.out):"
        tail -n 20 "${GATE_TMP}/fin25.out" | sed 's/^/    /'
        fail_fatal=1
    fi
}

# ---- Stage 4.8: master-mission E2E benchmarks (opt-in) ----------------------
# The flight-computer + battery-charger end-to-end protocols exercise the WHOLE
# ECAD+sim+firmware stack (parts -> schematic -> SPICE -> place/route/trace-opt ->
# antenna/openEMS -> board checks -> firmware+Renode sensor-in-the-loop). Heavy
# (~90-120s each, real openEMS/Renode), so OPT-IN like the other benches:
#   CHUDWARE_RUN_BENCH_FC=1   flight computer     CHUDWARE_RUN_BENCH_CHG=1  battery charger
# Legs that need an absent backend SKIP honestly; a real defect FAILs the gate.
_run_e2e_bench() {  # $1=label $2=script $3=tmp-subdir $4=gate-flag $5=script-env-prefix
    local label="$1" script="$2" pfx="$3" flag="$4" envpfx="$5"
    if [ "${!flag:-}" != "1" ]; then
        skip_stage optin "$label" "set ${flag}=1 to run the E2E benchmark" "${flag} not set"
        return 0
    fi
    if [ ! -f "$REPO/$script" ]; then
        skip_stage env "$label" "$script missing" "$script missing"; return 0
    fi
    local venv_py=""
    venv_py="$(gate_python_or_system)"
    if [ -z "$venv_py" ]; then skip_stage env "$label" "no python3" "no python3"; return 0; fi
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        for cand in "$REPO/core/build-occt-linux/bin/libchudware.so" \
                    "$REPO/core/build-linux/bin/libchudware.so"; do
            [ -f "$cand" ] && export CHUDWARE_LIBCHUDWARE="$cand" && break
        done
    fi
    local out="${GATE_TMP}/${pfx}"; mkdir -p "$out/markers"
    export "${envpfx}_OUT=$out" "${envpfx}_MARKERS=$out/markers"
    export CHUDWARE_ECAD_HEAVY_BUDGET_S="${CHUDWARE_ECAD_HEAVY_BUDGET_S:-300}"
    local rc=0
    if ! PYTHONPATH="$REPO:${PYTHONPATH:-}" "$venv_py" "$REPO/$script" \
            > "${GATE_TMP}/${pfx}.out" 2>&1; then rc=$?; fi
    local n_pass=0 n_fail=0 n_skip=0
    if [ -d "$out/markers" ]; then
        n_pass=$(grep -l '^PASS' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
        n_fail=$(grep -l '^FAIL' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
        n_skip=$(grep -l '^SKIP' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
    fi
    local detail="pass=$n_pass fail=$n_fail skip=$n_skip (see ${GATE_TMP}/${pfx}.out)"
    if [ "$rc" -eq 0 ] && [ "$n_fail" = "0" ]; then
        record "$label   PASS    $detail"; echo "  PASS ($detail)"
    else
        record "$label   FAIL    exit=$rc $detail"
        echo "  FAIL: $label (see ${GATE_TMP}/${pfx}.out):"
        tail -n 20 "${GATE_TMP}/${pfx}.out" | sed 's/^/    /'
        fail_fatal=1
    fi
}
stage_bench_flight_computer() {
    echo "=== [4.8/6] E2E benchmark: central flight computer ==="
    _run_e2e_bench "bench flight-comp" "scripts/bench_flight_computer.py" "bench_fc" "CHUDWARE_RUN_BENCH_FC" "CHUDWARE_FC"
}
stage_bench_battery_charger() {
    echo "=== [4.9/6] E2E benchmark: battery charger/controller ==="
    _run_e2e_bench "bench charger    " "scripts/bench_battery_charger.py" "bench_chg" "CHUDWARE_RUN_BENCH_CHG" "CHUDWARE_BC"
}

# ---- main -------------------------------------------------------------------
CORE_BUILD_OK=0
HOST_BUILD_OK=0
stage_check
stage_manual_gen
stage_ui_style_check
stage_core_build
stage_ui_abstract
stage_wasm_smoke
stage_ctest
stage_hostd_smoke
stage_web_parity
stage_appimage_smoke
stage_pytest
stage_vv
stage_modeling
stage_fin_content
stage_bench_flight_computer
stage_bench_battery_charger
stage_ui_imgui
stage_bench
stage_perf

echo
echo "============================================================"
echo "Chudware Linux gate — summary"
echo "============================================================"
printf '  %s\n' "${SUMMARY[@]}"
echo "------------------------------------------------------------"
if [ "$fail_fatal" -ne 0 ]; then
    echo "RESULT: FAIL (fatal stage failure)"
    exit 1
fi
if [ "${CHUDWARE_GATE_STRICT:-0}" = "1" ]; then
    echo "RESULT: PASS (CHUDWARE_GATE_STRICT=1 — no environment SKIP occurred; any SKIP above is an unset opt-in flag)"
else
    echo "RESULT: PASS (SKIPs are environment-only; check.py FAIL-RECORDED is pre-existing)"
fi
exit 0
