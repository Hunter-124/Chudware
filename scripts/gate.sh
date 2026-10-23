#!/usr/bin/env bash
# scripts/gate.sh — the Linux build + test gate for Chudware.
#
# Run it before pushing; CI runs it with CHUDWARE_GATE_STRICT=1. Stages, in execution order:
#   check.py           — source/audit gate using the provisioned runtime interpreter. FATAL.
#   manual regen clean — docs/manual/*.md drift vs tools/manual_gen (advisory).
#   core build         — libchudware with -DCHUDWARE_WITH_OCCT=ON. Environment SKIP when the
#                        system OCCT cmake config is not found.
#   host build         — cmake -S host -B host/build-linux + ninja (GUI + host test binaries).
#   wasm build         — ./build.sh wasm into a scratch dir (CHUDWARE_WEB_APP override); the
#                        same host/ tree compiled by Emscripten. FATAL when emsdk is provisioned.
#   ctest              — core ctest, only after a successful core build.
#   hostd smoke        — daemon lifecycle/race/stop/recovery (scripts/hostd_smoke.py).
#   web parity probe   — contracts/web_parity.md over a scratch hostd with the Python worker
#                        bridge disabled (scripts/web_parity_probe.py).
#   appimage smoke     — opt-in (CHUDWARE_GATE_APPIMAGE=1), advisory.
#   linux packages     — packaging suite (distro artifacts, signing, evidence, finalization).
#   pytest             — per-unit sweep of services/*, services/test_*.py, scripts, assets
#                        and tools with the service venv. Failures are FATAL.
#   vv-bench, modeling — opt-in (CHUDWARE_RUN_VV, CHUDWARE_RUN_MODELING).
#   sensor-node demo   — STM32 sensor-node board/firmware/thermal/power demo
#                        (scripts/demo_sensor_node.py); default on, Renode/OpenFOAM-gated
#                        legs SKIP honestly via marker files.
#   E2E benches        — opt-in (CHUDWARE_RUN_BENCH_FC, CHUDWARE_RUN_BENCH_CHG).
#   host shot          — CHUDWARE_SHOT=m1 + copilot_slash headless screenshots via xvfb-run.
#   perf               — opt-in (CHUDWARE_RUN_PERF).
# Then a PASS/SKIP/FAIL summary; exit 1 on any fatal stage failure.
#
# SKIP semantics: OCCT-absent, emsdk-absent and venv-absent are environment SKIPs, not
# failures — the gate stays green on a box that simply hasn't provisioned those optional
# runtimes, while still running the source/audit checks and available builds.
# That leniency is wrong in CI, where an unprovisioned runner reporting PASS having built
# nothing is a false green. Set CHUDWARE_GATE_STRICT=1 (linux-gate.yml does) to turn every
# environment SKIP into a fatal failure. See skip_stage() below.
set -euo pipefail

# COLMAP/FAISS and BLAS can nest independent native worker pools. Bound their
# defaults before Python imports either runtime; explicit caller limits win.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-${CHUDWARE_SIM_THREADS:-4}}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"

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
# Probe the provisioned service venv, then a repo-local .venv-mcp and the conventional
# local venv names, and export the answer once so every stage agrees on the runner. A
# stage that hardcoded a single path would SKIP on a box whose venv lives elsewhere and
# the gate would go green without running a single Python test — a false green.
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

# ---- check.py: source/audit gate ----------------------------------------------
# CHECK_PYTHON is deliberately separate from CHUDWARE_GATE_PYTHON: linux-gate
# provisions the audit's minimal runtime imports in a job-local venv,
# while the later pytest sweep needs the much larger service runtime venv.
stage_check() {
    echo "=== scripts/check.py (source/audit gate) ==="
    local check_py="${CHECK_PYTHON:-}"
    if [ -z "$check_py" ]; then
        check_py="$(command -v python3 2>/dev/null || true)"
    fi
    if [ -z "$check_py" ] || [ ! -x "$check_py" ]; then
        skip_stage env "check.py" "CHECK_PYTHON is not executable (${check_py:-unset})" \
            "no usable check.py interpreter"
        return 0
    fi
    echo "  interpreter: $check_py"
    if "$check_py" scripts/check.py > ${GATE_TMP}/check.out 2>&1; then
        record "check.py            PASS    all source/audit gates green"
        echo "  PASS"
    else
        local rc=$?
        record "check.py            FAIL    exit $rc (fatal — see ${GATE_TMP}/check.out)"
        fail_fatal=1
        echo "  FAIL (exit $rc) — ${GATE_TMP}/check.out:"
        tail -n 8 ${GATE_TMP}/check.out | sed 's/^/    /'
    fi
}

# ---- manual regen clean (advisory) --------------------------------------------
# Drift check for the auto-generated docs/manual/*.md (tools/manual_gen.py): regenerates the
# manual in-memory from the ribbon inventory + cad_guide prose + contracts summary and byte-
# compares it against the committed baseline. Advisory — never sets fail_fatal —
# because manual drift means "someone added a ribbon command or a contract and forgot to rerun
# `python -m tools.manual_gen`", not a build break.
stage_manual_gen() {
    echo "=== manual regen clean (advisory) ==="
    local manual_py
    manual_py="$(gate_python_or_system)"
    if [ -z "$manual_py" ]; then
        skip_stage env "manual regen clean" "no usable Python interpreter" "python3 not installed"
        return 0
    fi
    if "$manual_py" -m tools.manual_gen --check > ${GATE_TMP}/manual.out 2>&1; then
        record "manual regen clean  PASS    docs/manual/*.md matches a fresh regen"
        echo "  PASS"
    else
        record "manual regen clean  FAIL-RECORDED  drift or missing coverage (non-fatal — see ${GATE_TMP}/manual.out)"
        echo "  FAIL (non-fatal) — ${GATE_TMP}/manual.out:"
        tail -n 8 ${GATE_TMP}/manual.out | sed 's/^/    /'
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

# ---- core build: core CMake/Ninja build ---------------------------------------
stage_core_build() {
    echo "=== core CMake/Ninja build (libchudware, CHUDWARE_WITH_OCCT=ON) ==="
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
    # ~16x slower (sim ctests 34s -> 2s once Release was set).
    if ! CCACHE_DISABLE=1 cmake -S core -B "$build_dir" -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_WITH_OCCT=ON -G Ninja > ${GATE_TMP}/cfg.out 2>&1; then
        record "core build          FAIL    cmake configure failed (see ${GATE_TMP}/cfg.out)"
        echo "  FAIL: configure"
        # FetchContent's actual download/clone error precedes CMake's final
        # stack trace. Keep the complete configure log in CI's job log.
        cat "${GATE_TMP}/cfg.out"
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

# ---- host build: host GUI + test build (default native targets) ---------------
# host/ is the single cross-platform GUI target. Build the default target set:
# Chudware alone does not build the host tests that linux-gate.yml runs with ctest.
stage_host_build() {
    echo "=== host GUI + test build (ninja default targets) ==="
    if ! command -v cmake >/dev/null 2>&1 || ! command -v ninja >/dev/null 2>&1; then
        skip_stage env "host build" "cmake or ninja not on PATH" "cmake/ninja missing"
        return 0
    fi
    local host_build="$REPO/host/build-linux"
    local cmake_args=(-S "$REPO/host" -B "$host_build" -G Ninja -DCMAKE_BUILD_TYPE=Release)
    # CI keeps the 1.5 GB SDK outside the checkout so actions/checkout cannot
    # delete it. linux-gate exports CEF_ROOT; pass it as a CMake cache value
    # instead of relying on a gitignored symlink inside extern/cef.
    if [ -n "${CEF_ROOT:-}" ]; then
        cmake_args+=("-DCEF_ROOT=$CEF_ROOT")
    fi
    if ! CCACHE_DISABLE=1 cmake "${cmake_args[@]}" > ${GATE_TMP}/host_cfg.out 2>&1; then
        record "host build          FAIL    cmake configure (see ${GATE_TMP}/host_cfg.out)"
        echo "  FAIL: configure"; tail -n 15 ${GATE_TMP}/host_cfg.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    if ! ninja -C "$host_build" > ${GATE_TMP}/host_build.out 2>&1; then
        record "host build          FAIL    ninja default targets (see ${GATE_TMP}/host_build.out)"
        echo "  FAIL: build"; tail -n 20 ${GATE_TMP}/host_build.out | sed 's/^/    /'
        fail_fatal=1
        return 0
    fi
    record "host build          PASS    host/build-linux GUI + test binaries built"
    echo "  PASS"
    HOST_BUILD_OK=1
}

# ---- wasm build: WASM web-client build smoke ----------------------------------
# The web app is the same host/ tree compiled by Emscripten; a native-only break of the
# WASM build is a web-parity regression, so this stage is FATAL when the toolchain is
# provisioned and the build breaks. SKIPs honestly when emsdk is absent. Stages into
# $GATE_TMP/wasm-app via CHUDWARE_WEB_APP so build.sh's default web-app stage
# (host/build-wasm/app) is never clobbered.
stage_wasm_smoke() {
    echo "=== WASM web-client build smoke ==="
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

# ---- ctest (core) -------------------------------------------------------------
stage_ctest() {
    echo "=== ctest (core) ==="
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

# ---- hostd smoke: chudware-hostd lifecycle ------------------------------------
# The shared engine-host daemon (core/hostd): spawn -> connect -> engine call ->
# idle-exit, dual-client race -> one daemon, --stop, kill -9 stale-socket
# recovery. Uses a scratch CHUDWARE_INSTANCE_KEY so a live dev/installed daemon is
# never touched. FATAL when the binary exists and the suite fails.
stage_hostd_smoke() {
    echo "=== hostd smoke ==="
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

# ---- web parity probe (contracts/web_parity.md) -------------------------------
# Spawns a scratch chudware-hostd with the Python worker bridge disabled (exactly what the
# WASM web client faces) and asserts contracts/web_parity.md: web_required methods are
# libchudware-served, web_unsupported gaps fail loud. FATAL when a hostd binary exists —
# a new parity hole (or an unrecorded cutover) must never merge silently.
stage_web_parity() {
    echo "=== web parity probe ==="
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

# ---- appimage smoke (advisory, opt-in) ----------------------------------------
# Builds the AppImage (wheelhouse skipped for speed — provisioning isn't what this
# stage tests) and boots it headless in daemon mode: screenshot must render, exit 0,
# and the bundled chudware-hostd must idle-exit after its linger. ADVISORY: failures are
# RECORDED as WARN, never fatal (packaging tooling + xvfb are machine-dependent).
# Opt in with CHUDWARE_GATE_APPIMAGE=1. A scratch CHUDWARE_INSTANCE_KEY isolates the run
# from any live dev/installed daemon; leftover detection and cleanup go through
# scripts/hostd_scope.py, which matches only this user's hostd carrying exactly that key
# and started after this stage's mark — never a name/path pgrep or pkill.
stage_appimage_smoke() {
    echo "=== AppImage smoke (advisory) ==="
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
    local shot=${GATE_TMP}/appimage_shot.png key="gateimg$$-${RANDOM}${RANDOM}" since
    rm -f "$shot"
    if ! since="$(python3 scripts/hostd_scope.py now)"; then
        record "appimage smoke      WARN    cannot mark run start for scoped hostd checks"
        echo "  WARN: hostd_scope.py now failed"; return 0
    fi
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
    if ! leftover="$(python3 scripts/hostd_scope.py list --key "$key" --since "$since")"; then
        record "appimage smoke      WARN    cannot inspect this run's chudware-hostd"
        echo "  WARN: hostd_scope.py list failed"; return 0
    fi
    if [ -n "$leftover" ]; then
        record "appimage smoke      WARN    chudware-hostd still running after linger+grace"
        echo "  WARN: leftover daemon:"; echo "$leftover" | sed 's/^/    /'
        python3 scripts/hostd_scope.py reap --key "$key" --since "$since" 2>&1 | sed 's/^/    /' || true
        return 0
    fi
    record "appimage smoke      PASS    built $(basename "$img") + xvfb screenshot + daemon idle-exit"
    echo "  PASS ($(du -h "$img" | cut -f1) image, shot at $shot)"
}

# ---- linux packages: Linux distro packaging -----------------------------------
# packaging/test_linux_packages.py builds .deb and .rpm from a small synthetic
# AppDir and asserts the ar member order, the control/spec metadata, the
# prerelease version sanitisation packaging/artifact_names.py's canonical_artifact() agrees
# with, byte-for-byte determinism, and that packaging never mutates the signed
# payload. No network, ~2s. Unlike the AppImage smoke above this needs no GUI,
# no Xvfb and no real build tree, so it runs by default rather than opt-in.
# rpmbuild is optional: the suite skips its rpm cases without it.
# The same stage also covers release signing, evidence and finalization; all use
# the selected service interpreter rather than requiring system Python's pytest.
stage_linux_packages() {
    echo "=== Linux distro packaging ==="
    if [ ! -f packaging/test_linux_packages.py ]; then
        skip_stage env "linux packages" "packaging/test_linux_packages.py missing" "suite not present"
        return 0
    fi
    if ! resolve_gate_python; then
        skip_stage env "linux packages" "no venv with pytest found" "no venv with pytest available"
        return 0
    fi
    if "$GATE_PY" -m pytest -q packaging \
            > ${GATE_TMP}/linux_packages.out 2>&1; then
        record "linux packages     PASS    $(tail -n 1 ${GATE_TMP}/linux_packages.out)"
        echo "  PASS ($(tail -n 1 ${GATE_TMP}/linux_packages.out))"
        return 0
    fi
    record "linux packages     FAIL    packaging suite failed (see ${GATE_TMP}/linux_packages.out)"
    echo "  FAIL"; tail -n 20 ${GATE_TMP}/linux_packages.out | sed 's/^/    /'
    fail_fatal=1
}

# ---- pytest: per-unit Python sweep --------------------------------------------
# Uses the service venv discovered by resolve_gate_python() (the provisioned Chudware
# venv, then the repo-local .venv-mcp). Emits a loud environment SKIP when none is found
# so engineers know the gate is not running real suites.
#
# CHUDWARE_LIBCHUDWARE is set to the built libchudware.so when it exists and the caller
# has not already set it. Without a loadable .so core-calling tests skip or deselect; the
# stage still runs and collects — it just won't exercise core-calling paths.
#
# FATAL: any failed test (pytest rc=1), a collection crash or internal error (rc>=2), a
# services enumeration gap, or a sweep that collected zero tests. A single unit with no
# tests (rc=5) is an environment SKIP. The sweep runs one pytest invocation per unit —
# per top-level directory, and for `services/` one per subpackage (plus its few
# root-level test files) — to avoid the cross-suite import-order pollution failures of a
# combined run (pytest's import ordering differs per-dir vs. combined).
stage_pytest() {
    echo "=== pytest (per-unit Python sweep) ==="

    # --- locate the python runner (see resolve_gate_python above) ---
    local venv_py=""
    if resolve_gate_python; then
        venv_py="$GATE_PY"
        echo "  runner: $GATE_PY_LABEL"
    else
        skip_stage env "pytest" "no venv with pytest found; provision ~/.local/share/Chudware/mcp-venv or set CHUDWARE_GATE_PYTHON" "no venv with pytest available (service venv not provisioned)"
        return 0
    fi

    # Exercise the core this gate just built, not an older opt-in OCCT build tree.
    # An explicit CHUDWARE_LIBCHUDWARE still selects an external dispatcher.
    local so_path=""
    for cand in "$REPO/core/build-linux/bin/libchudware.so" "$REPO/core/build-occt-linux/bin/libchudware.so"; do
        [ -f "$cand" ] && { so_path="$cand"; break; }
    done
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ] && [ -n "$so_path" ]; then
        export CHUDWARE_LIBCHUDWARE="$so_path"
        echo "  CHUDWARE_LIBCHUDWARE=$CHUDWARE_LIBCHUDWARE (auto-detected)"
    elif [ -n "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        echo "  CHUDWARE_LIBCHUDWARE=$CHUDWARE_LIBCHUDWARE (from environment)"
    else
        echo "  CHUDWARE_LIBCHUDWARE not set and no libchudware.so under core/build-{occt-,}linux/bin; core-calling tests will skip/deselect"
    fi

    if [ "${CORE_BUILD_OK:-0}" = 1 ] && [ -z "${CHUDWARE_MFG_LANE_TEST_BINARY:-}" ]; then
        export CHUDWARE_MFG_LANE_TEST_BINARY="$REPO/core/build-linux/bin/test_mfg_lane"
        if [ ! -x "$CHUDWARE_MFG_LANE_TEST_BINARY" ]; then
            record "pytest             FAIL    freshly built manufacturing lane fixture is missing"
            fail_fatal=1
            return 1
        fi
    fi

    # --- per-unit sweep ---
    # One invocation per unit avoids cross-suite import-order pollution.
    #
    # `services/` is swept one level deeper than the other top-level directories.
    # Process isolation keeps registry mutations and import ordering in one service
    # suite from changing another suite's results. It does not relax failure
    # handling: every collected test failure and collection crash remains fatal.
    # The coverage guard below ensures the split does not silently lose test files.
    #
    # The unit list is DERIVED at runtime (glob), never hardcoded, so a new services
    # subpackage is picked up automatically and cannot silently go unrun. The
    # coverage guard below turns any enumeration gap into a hard failure.
    local dirs=()
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
    dirs+=("tools" "scripts" "assets")

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
    local any_test_fail=0

    for dir in "${dirs[@]}"; do
        if [ ! -e "$dir" ]; then
            # Every unit is either globbed from disk or a tracked top-level dir, so a
            # missing one means the sweep list is stale — never a silent skip.
            echo "  $dir: path not found"
            record "pytest/$dir         FAIL    sweep unit does not exist (stale unit list)"
            any_hard_fail=1
            continue
        fi

        local out_file="${GATE_TMP}/pytest_${dir//\//_}.out"

        echo -n "  $dir ... "
        local rc=0
        "$venv_py" -m pytest "$dir" -q --tb=short >"$out_file" 2>&1 || rc=$?

        # exit 5 = no tests collected in this unit — an environment SKIP, not a pass
        if [ "$rc" -eq 5 ]; then
            echo "no tests collected (SKIP)"
            skip_stage env "pytest/$dir" "no tests collected in $dir"
            continue
        fi

        # Runner temp is removed after the job. Preserve failures in unmetered
        # CI logs, including collection crashes, without artifacts or a rerun.
        if [ "$rc" -ne 0 ]; then
            printf '\n--- pytest failure diagnostics: %s (exit %s) ---\n' "$dir" "$rc"
            cat "$out_file"
            printf '%s\n' '--- end pytest failure diagnostics ---'
        fi

        # Parse summary line: pytest outputs "N passed, M failed, K errors in Xs"
        local summary_line
        summary_line="$(grep -E '[0-9]+ (passed|failed|errors?|skipped|xfailed|xpassed)' "$out_file" | tail -1 || true)"
        if [ -z "$summary_line" ]; then
            echo "HARD-FAIL (pytest exit $rc without a test summary)"
            record "pytest/$dir         FAIL    pytest exit $rc without a test summary (see $out_file)"
            any_hard_fail=1
            continue
        fi
        local collected
        collected="$(printf '%s\n' "$summary_line" | \
                     grep -oE '[0-9]+ (passed|failed|errors?|skipped|xfailed|xpassed)' | \
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
            # rc=1 = collection succeeded and one or more tests failed
            echo "FAIL  ($summary_line)"
            record "pytest/$dir         FAIL    $summary_line (see $out_file)"
            any_test_fail=1
        else
            # rc=2+ = collection crash or internal error → hard failure
            echo "HARD-FAIL (rc=$rc)"
            record "pytest/$dir         FAIL    collection crash rc=$rc (see $out_file)"
            any_hard_fail=1
        fi
    done

    echo "  total collected ≈ $total_collected tests across ${#dirs[@]} units"
    record "pytest              total ≈ $total_collected collected | $total_passed passed | ${#dirs[@]} units"

    # Zero tests collected across every sweep unit is always a harness break (wrong cwd,
    # renamed services dir, broken interpreter), never an environment SKIP: the sweep
    # dirs are tracked in-repo.
    if [ "$total_collected" -eq 0 ]; then
        record "pytest              FAIL    zero tests collected across ${#dirs[@]} sweep units — the suite did not run (harness/cwd break, not an environment SKIP)"
        echo "  FAIL: zero tests collected — the Python suites did not run at all"
        fail_fatal=1
    fi

    if [ "$any_test_fail" -ne 0 ]; then
        record "pytest              FAIL    one or more units had failing tests (rc=1)"
        echo "  FAIL: failing tests in at least one unit"
        fail_fatal=1
    fi

    if [ "$any_hard_fail" -ne 0 ]; then
        record "pytest              FAIL    one or more units crashed in collection/internals (rc≥2) or do not exist"
        echo "  FAIL: hard collection error or missing unit in at least one dir"
        fail_fatal=1
    fi
}

# ---- host shot: host GUI headless screenshot smoke ----------------------------
# Runs the host GUI headless under xvfb and verifies it renders a screenshot
# (CHUDWARE_SHOT=m1), then the copilot slash-autocomplete scenario. The binary is
# expected at host/build-linux/bin/chudware (built by stage_host_build, or a prior
# cached build). Environment SKIP when the host build did not succeed and the binary
# is absent; a missing binary after a successful build is FATAL.
stage_host_shot() {
    echo "=== host GUI shot smoke (CHUDWARE_SHOT=m1) ==="
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
    local libchudware="${CHUDWARE_LIBCHUDWARE:-$REPO/core/build-linux/bin/libchudware.so}"
    # Chats are project-local; a private HOME alone does not isolate a shot.
    local shot_root="${GATE_TMP}/host-shot-state"
    mkdir -p "$shot_root/home" "$shot_root/work"
    local shot_env=(env HOME="$shot_root/home" APPDATA="$shot_root/data"
        XDG_DATA_HOME="$shot_root/data" CHUDWARE_NO_FILE_DIALOGS=1)
    local shot_py=""
    shot_py="$(gate_python_or_system)"
    [ -n "$shot_py" ] && shot_env+=(CHUDWARE_PYTHON="$shot_py")
    [ -f "$libchudware" ] && shot_env+=(CHUDWARE_LIBCHUDWARE="$libchudware")
    local headless=()
    if [ "${CHUDWARE_SHOT_HEADED:-0}" != "1" ] && command -v xvfb-run >/dev/null 2>&1; then
        headless=(xvfb-run -a)
    fi
    if ! (cd "$shot_root/work" &&
        "${headless[@]}" "${shot_env[@]}" CHUDWARE_INSTANCE_KEY="gate-m1-$BASHPID" \
        CHUDWARE_SHOT=m1 "$bin" --ui-screenshot "$shot") > ${GATE_TMP}/host_shot.out 2>&1; then
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
    if ! (cd "$shot_root/work" &&
        "${headless[@]}" "${shot_env[@]}" CHUDWARE_INSTANCE_KEY="gate-slash-$BASHPID" \
        CHUDWARE_SHOT=copilot_slash "$bin" --ui-screenshot "$slash_shot") > ${GATE_TMP}/copilot_slash.out 2>&1; then
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

# ---- perf-regression: modeling perf (tools/perf_regression.py) ----------------
# Times the kernel against CHUDWARE_PERF_BASELINE (default tools/perf_baseline.json, a
# machine-local record the first run creates; it is never committed).
# Env-gated (perf timings are machine + load sensitive, so this is NOT a hard blocker by
# default): set CHUDWARE_RUN_PERF=1 to run. A regression is RECORDED (non-fatal) unless
# CHUDWARE_PERF_FATAL=1. Needs a loadable .so — prefers this gate's core/build-linux
# engine; falls back to whatever core_bridge
# resolves. Capture a baseline on the same machine/thread environment with
# `python tools/perf_regression.py --baseline <path> --update`; legacy records are rejected.
stage_perf() {
    echo "=== modeling perf-regression ==="
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
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ] && [ -f "$REPO/core/build-linux/bin/libchudware.so" ]; then
        export CHUDWARE_LIBCHUDWARE="$REPO/core/build-linux/bin/libchudware.so"
    fi
    local rc=0
    CHUDWARE_ENGINE_DAEMON=0 "$perf_py" "$REPO/tools/perf_regression.py" --iters "${CHUDWARE_PERF_ITERS:-5}" \
        --baseline "${CHUDWARE_PERF_BASELINE:-$REPO/tools/perf_baseline.json}" \
        --json ${GATE_TMP}/perf.json > ${GATE_TMP}/perf.out 2>&1 || rc=$?
    tail -n 16 ${GATE_TMP}/perf.out | sed 's/^/    /'
    if [ "$rc" -eq 0 ]; then
        record "perf-regression     PASS    no median regressions vs baseline"
        echo "  PASS"
    elif [ "$rc" -eq 1 ]; then
        record "perf-regression     $([ "${CHUDWARE_PERF_FATAL:-0}" = 1 ] && echo FAIL || echo WARN)    median regression vs baseline (see ${GATE_TMP}/perf.out)"
        echo "  REGRESSION detected"
        [ "${CHUDWARE_PERF_FATAL:-0}" = 1 ] && fail_fatal=1
    elif [ "$rc" -eq 2 ]; then
        skip_stage env "perf-regression" "engine unavailable (rc=$rc)" "engine unavailable"
    else
        record "perf-regression     FAIL    invalid measurement or harness failure (rc=$rc; see ${GATE_TMP}/perf.out)"
        fail_fatal=1
    fi
}

# ---- vv-bench: V&V benchmark smoke --------------------------------------------
# Optional, env-gated SKIP: runs the V&V benchmark harness in smoke mode. Smoke needs no
# .so; its native-RPC cases (hydraulic/piping/ECAD) skip with the loader reason unless a
# libchudware.so is found (the stage wires core/build-linux when CHUDWARE_LIBCHUDWARE is unset).
# Default is SKIP (stays green) so the gate does not depend on external deps;
# set CHUDWARE_RUN_VV=smoke (or =full) to opt in.
# - CHUDWARE_RUN_VV=smoke  → pytest scripts/vv_benchmark.py::TestSmoke  (fast; .so optional)
# - CHUDWARE_RUN_VV=full   → pytest scripts/vv_benchmark.py  (all suites, needs .so + deps)
stage_vv() {
    echo "=== V&V benchmark smoke ==="
    if [ -z "${CHUDWARE_RUN_VV:-}" ]; then
        skip_stage optin "vv-bench" "set CHUDWARE_RUN_VV=smoke or =full to run V&V harness" "CHUDWARE_RUN_VV not set"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "vv-bench" "python3 not on PATH" "python3 not installed"
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
        record "vv-bench            PASS    $summary_line"
        echo "  PASS ($summary_line)"
    else
        local rc=$?
        if [ "$rc" -eq 5 ]; then
            skip_stage env "vv-bench" "no tests collected (check venv + deps)" "no tests collected"
        else
            record "vv-bench            FAIL    pytest exit $rc (see ${GATE_TMP}/vv.out)"
            echo "  FAIL: V&V suite (see ${GATE_TMP}/vv.out):"
            tail -n 15 ${GATE_TMP}/vv.out | sed 's/^/    /'
            fail_fatal=1
        fi
    fi
}

# ---- modeling: modeling-op combination battery --------------------------------
# Optional, env-gated SKIP: drives the C++ feature engine through every registered
# modeling op + a pairwise/sequence matrix and asserts on live geometry. Default is
# SKIP (stays green) so the normal gate never depends on the OCCT .so; opt in with:
# - CHUDWARE_RUN_MODELING=smoke → pytest ...::TestSmoke  (registry integrity, no .so)
# - CHUDWARE_RUN_MODELING=full  → full live battery       (needs OCCT libchudware.so)
# NOTE: full mode needs the OCCT geometry kernel (core/build-occt-linux/bin/libchudware.so);
# the plain build-linux .so has no geometry and would ALL-SKIP (which the battery fails).
stage_modeling() {
    echo "=== Modeling-op combination battery ==="
    if [ -z "${CHUDWARE_RUN_MODELING:-}" ]; then
        skip_stage optin "modeling" "set CHUDWARE_RUN_MODELING=smoke or =full" "CHUDWARE_RUN_MODELING not set"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        skip_stage env "modeling" "python3 not on PATH" "python3 not installed"
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
        record "modeling            PASS    $summary_line"
        echo "  PASS ($summary_line)"
    else
        local rc=$?
        if [ "$rc" -eq 5 ]; then
            skip_stage env "modeling" "no tests collected (check venv + deps)" "no tests collected"
        else
            record "modeling            FAIL    pytest exit $rc (see ${GATE_TMP}/modeling.out)"
            echo "  FAIL: modeling battery (see ${GATE_TMP}/modeling.out):"
            tail -n 15 ${GATE_TMP}/modeling.out | sed 's/^/    /'
            fail_fatal=1
        fi
    fi
}

# ---- sensor-node demo: STM32 sensor-node board/firmware/thermal/power ---------
# Default ON (bounded <5 min). Legs that need Renode/OpenFOAM write SKIP markers
# when those binaries are absent — never fake a pass. Opt out with CHUDWARE_SKIP_SENSOR_DEMO=1.
# Fast dry thermal: CHUDWARE_SENSOR_DEMO_THERMAL_DRY=1. Artifacts under $GATE_TMP/sensor_node_demo/.
stage_sensor_node_demo() {
    echo "=== sensor-node demo ==="
    if [ "${CHUDWARE_SKIP_SENSOR_DEMO:-}" = "1" ]; then
        skip_stage optin "sensor-node demo" "CHUDWARE_SKIP_SENSOR_DEMO=1" "CHUDWARE_SKIP_SENSOR_DEMO=1"
        return 0
    fi
    if [ ! -f "$REPO/scripts/demo_sensor_node.py" ]; then
        skip_stage env "sensor-node demo" "scripts/demo_sensor_node.py missing" "demo script missing"
        return 0
    fi

    local venv_py=""
    venv_py="$(gate_python_or_system)"
    if [ -z "$venv_py" ]; then
        skip_stage env "sensor-node demo" "no python3" "python3 not installed"
        return 0
    fi

    # Pin a sim-enabled engine when present (motion/thermal FEA); never invent one.
    if [ -z "${CHUDWARE_LIBCHUDWARE:-}" ]; then
        for cand in "$REPO/core/build-occt-linux/bin/libchudware.so" \
                    "$REPO/core/build-linux/bin/libchudware.so"; do
            [ -f "$cand" ] && export CHUDWARE_LIBCHUDWARE="$cand" && break
        done
    fi

    local out="${GATE_TMP}/sensor_node_demo"
    mkdir -p "$out"
    export CHUDWARE_SENSOR_DEMO_OUT="$out"
    export CHUDWARE_SENSOR_DEMO_MARKERS="$out/markers"
    export CHUDWARE_PROJECT_DIR="${CHUDWARE_PROJECT_DIR:-$out/project}"

    local rc=0
    PYTHONPATH="$REPO:${PYTHONPATH:-}" \
            "$venv_py" "$REPO/scripts/demo_sensor_node.py" \
            > "${GATE_TMP}/sensor_node_demo.out" 2>&1 || rc=$?

    # Summarise per-leg markers (PASS/FAIL/SKIP) for the gate line.
    local n_pass=0 n_fail=0 n_skip=0
    if [ -d "$out/markers" ]; then
        n_pass=$(grep -l '^PASS' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
        n_fail=$(grep -l '^FAIL' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
        n_skip=$(grep -l '^SKIP' "$out/markers"/*.marker 2>/dev/null | wc -l | tr -d ' ' || true)
    fi
    local detail="pass=$n_pass fail=$n_fail skip=$n_skip (see ${GATE_TMP}/sensor_node_demo.out)"

    if [ "$rc" -eq 0 ] && [ "$n_fail" = "0" ]; then
        record "sensor-node demo    PASS    $detail"
        echo "  PASS ($detail)"
    else
        record "sensor-node demo    FAIL    exit=$rc $detail"
        echo "  FAIL: sensor-node demo (see ${GATE_TMP}/sensor_node_demo.out):"
        tail -n 20 "${GATE_TMP}/sensor_node_demo.out" | sed 's/^/    /'
        fail_fatal=1
    fi
}

# ---- E2E benches: master-mission E2E benchmarks (opt-in) ----------------------
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
    PYTHONPATH="$REPO:${PYTHONPATH:-}" "$venv_py" "$REPO/$script" \
            > "${GATE_TMP}/${pfx}.out" 2>&1 || rc=$?
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
    echo "=== E2E benchmark: central flight computer ==="
    _run_e2e_bench "bench flight-comp" "scripts/bench_flight_computer.py" "bench_fc" "CHUDWARE_RUN_BENCH_FC" "CHUDWARE_FC"
}
stage_bench_battery_charger() {
    echo "=== E2E benchmark: battery charger/controller ==="
    _run_e2e_bench "bench charger    " "scripts/bench_battery_charger.py" "bench_chg" "CHUDWARE_RUN_BENCH_CHG" "CHUDWARE_BC"
}

# ---- main -------------------------------------------------------------------
CORE_BUILD_OK=0
HOST_BUILD_OK=0
stage_check
stage_manual_gen
stage_core_build
stage_host_build
stage_wasm_smoke
stage_ctest
stage_hostd_smoke
stage_web_parity
stage_appimage_smoke
stage_linux_packages
stage_pytest
stage_vv
stage_modeling
stage_sensor_node_demo
stage_bench_flight_computer
stage_bench_battery_charger
stage_host_shot
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
    echo "RESULT: PASS (each SKIP above is a missing environment capability or an unset opt-in flag; WARN/FAIL-RECORDED rows come from advisory stages)"
fi
exit 0
