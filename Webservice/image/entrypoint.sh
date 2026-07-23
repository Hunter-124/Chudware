#!/usr/bin/env bash
# entrypoint.sh — Xvfb + Chudware + stream backend (novnc default, selkies optional)
set -euo pipefail

log() { echo "[chudware-session $(date -u +%H:%M:%S)] $*" >&2; }

# ---- Contract defaults ------------------------------------------------------
export DISPLAY="${DISPLAY:-:0}"
export HOME="${HOME:-/home/user}"
export CHUDWARE_PLM_DIR="${CHUDWARE_PLM_DIR:-/plm}"
export STREAM_BACKEND="${STREAM_BACKEND:-novnc}"   # novnc | selkies
export SELKIES_ENCODER="${SELKIES_ENCODER:-x264enc}"
export CHUDWARE_HEAVY="${CHUDWARE_HEAVY:-0}"
export CHUDWARE_USER_ID="${CHUDWARE_USER_ID:-${CHUDWARE_USER_EMAIL:-user}}"
export CHUDWARE_USER_ID="${CHUDWARE_USER_ID:-$CHUDWARE_USER_ID}"
export CHUDWARE_USER_DISPLAY="${CHUDWARE_USER_DISPLAY:-${CHUDWARE_USER_DISPLAY:-User}}"
export CHUDWARE_USER_DISPLAY="${CHUDWARE_USER_DISPLAY:-$CHUDWARE_USER_DISPLAY}"

# CEF: compile-time linked; lazy cold-init reduces container start risk.
# no_sandbox already set in cef_platform_linux.cpp; single-process default on.
export CHUDWARE_CEF_LAZY="${CHUDWARE_CEF_LAZY:-1}"
export CHUDWARE_CEF_TRACE="${CHUDWARE_CEF_TRACE:-0}"

CHUDWARE_ROOT="${CHUDWARE_ROOT:-/opt/chudware}"
export PATH="$CHUDWARE_ROOT/bin:$PATH"
export LD_LIBRARY_PATH="$CHUDWARE_ROOT/lib:$CHUDWARE_ROOT/bin:${LD_LIBRARY_PATH:-}"
# Explicit path so cpp_core ensureLoaded finds the engine even if dlopen
# candidates miss (see services/worker/cpp_core.cpp).
export CHUDWARE_LIBCHUDWARE="${CHUDWARE_LIBCHUDWARE:-$CHUDWARE_ROOT/bin/libchudware.so}"

# Python AI worker (bootstrap.cpp runtimePython/appDir). Venv is image-layer
# under /opt/chudware — never under HOME (gateway bind-mounts HOME).
# Provider keys (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY,
# XAI_API_KEY, GEMINI_API_KEY, …) are passed through by the gateway; we never
# unset or filter *_API_KEY from the environment.
export CHUDWARE_PYTHON="${CHUDWARE_PYTHON:-$CHUDWARE_ROOT/runtime/venv/bin/python}"
export CHUDWARE_REPO="${CHUDWARE_REPO:-$CHUDWARE_ROOT/app}"
export CHUDWARE_PROMPTS_DIR="${CHUDWARE_PROMPTS_DIR:-$CHUDWARE_ROOT/app/agent/prompts}"

# Screen geometry — matches Xvfb; Chudware is told to fill it (CHUDWARE_WIN_SIZE,
# app.cpp) since there is no window manager to maximize into.
SCREEN_W="${SCREEN_W:-1920}"
SCREEN_H="${SCREEN_H:-1080}"
SCREEN_D="${SCREEN_D:-24}"
STREAM_PORT="${STREAM_PORT:-8080}"
export CHUDWARE_WIN_SIZE="${CHUDWARE_WIN_SIZE:-${SCREEN_W}x${SCREEN_H}}"
VNC_PORT="${VNC_PORT:-5900}"

# HOME may be a bind-mount; don't fail the seat if mkdir races or perms lag.
mkdir -p "$HOME" 2>/dev/null || true
mkdir -p "$HOME/.local/share" 2>/dev/null || true
mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
cd "$HOME" 2>/dev/null || cd /

# ---- PLM bootstrap (T-WEB2 §Q2.2): clone or start pull-only; NEVER push -----
# Shared bare remotes live under CHUDWARE_PLM_DIR (/plm). Private working trees
# land in $HOME/Projects/<name>. Failures log + continue so the seat still boots.
plm_bootstrap() {
    local plm_root="${CHUDWARE_PLM_DIR:-/plm}"
    local py="${CHUDWARE_PYTHON:-}"
    local repo="${CHUDWARE_REPO:-}"
    if [ ! -d "$plm_root" ]; then
        log "[plm] no $plm_root mount — skip bootstrap"
        return 0
    fi
    if [ -z "$py" ] || [ ! -x "$py" ]; then
        log "[plm] WARN CHUDWARE_PYTHON missing/unusable — skip bootstrap"
        return 0
    fi
    if [ -z "$repo" ] || [ ! -d "$repo" ]; then
        log "[plm] WARN CHUDWARE_REPO missing — skip bootstrap"
        return 0
    fi
    mkdir -p "$HOME/Projects" 2>/dev/null || true
    local bare name dest
    shopt -s nullglob
    for bare in "$plm_root"/*.git; do
        name="$(basename "$bare" .git)"
        dest="$HOME/Projects/$name"
        if [ ! -d "$dest/.aicadvault" ]; then
            log "[plm] cloning $name from $bare → $dest"
            if PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" "$py" -c "
import sys
from services.plm.remote import clone_from_remote
r = clone_from_remote(sys.argv[1], sys.argv[2])
if not r.get('ok'):
    print(r.get('reason','error') + ((': ' + r['hint']) if r.get('hint') else ''), file=sys.stderr)
    sys.exit(1)
" "$bare" "$dest" 2>&1; then
                log "[plm] cloned $name"
            else
                log "[plm] WARN clone failed for $name (seat continues)"
            fi
        else
            log "[plm] pull-only sync $name"
            # Pull/FF only — never CAS-push on seat start (advisor Q2.3).
            if PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" "$py" -c "
import sys
from pathlib import Path
from services.plm.remote import (
    _open_vault_repo, _fetch_folder, _remote_head_from_refs, _branch_tip,
    _is_ancestor, _ensure_branch_and_head, _materialize_working_tree,
)
from services.plm.vault import Vault, VaultError

project = Path(sys.argv[1])
remote_name = 'origin'
try:
    v = Vault.open(project)
    try:
        m = v.manifest()
    finally:
        v.close()
    entry = None
    for r in m.get('remotes') or []:
        if r.get('name') == remote_name:
            entry = r
            break
    if entry is None:
        for r in m.get('remotes') or []:
            if r.get('kind') == 'folder':
                entry = r
                break
    if entry is None or not entry.get('url'):
        print('no origin remote in manifest', file=sys.stderr)
        sys.exit(2)
    remote_url = entry['url']
    repo = _open_vault_repo(project)
    try:
        local_head = _branch_tip(repo)
        remote_refs = _fetch_folder(repo, remote_url, remote_name)
        remote_head = _remote_head_from_refs(remote_refs)
        if remote_head is None or local_head == remote_head:
            sys.exit(0)
        if local_head is None or _is_ancestor(repo, local_head, remote_head):
            _ensure_branch_and_head(repo, remote_head)
            _materialize_working_tree(project, repo, remote_head)
            sys.exit(0)
        # ahead or diverged: leave local alone; never push at start
        sys.exit(0)
    finally:
        repo.close()
except VaultError as e:
    print(f'{e.reason}: {e.hint}', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'{type(e).__name__}: {e}', file=sys.stderr)
    sys.exit(1)
" "$dest" 2>&1; then
                log "[plm] synced $name"
            else
                log "[plm] WARN sync failed for $name (seat continues)"
            fi
        fi
    done
    shopt -u nullglob
}
plm_bootstrap


PIDS=()
cleanup() {
    log "SIGTERM/EXIT — shutting down (${#PIDS[@]} children)"
    # Reverse order: streamer, chudware, vnc, xvfb
    local pid
    for ((i=${#PIDS[@]}-1; i>=0; i--)); do
        pid="${PIDS[$i]}"
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    sleep 1
    for ((i=${#PIDS[@]}-1; i>=0; i--)); do
        pid="${PIDS[$i]}"
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
    # Xvfb may leave a lock
    rm -f "/tmp/.X${DISPLAY#:}-lock" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- Xvfb -------------------------------------------------------------------
log "starting Xvfb on $DISPLAY ${SCREEN_W}x${SCREEN_H}x${SCREEN_D}"
Xvfb "$DISPLAY" -screen 0 "${SCREEN_W}x${SCREEN_H}x${SCREEN_D}" \
    -ac +extension GLX +render -noreset \
    -nolisten tcp &
PIDS+=($!)
# Wait for the display socket
for i in $(seq 1 50); do
    if [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then break; fi
    sleep 0.1
done
if [ ! -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
    log "FATAL: Xvfb failed to create display socket"
    exit 1
fi
log "Xvfb ready"

# ---- GL path: VirtualGL EGL for heavy+GPU, else llvmpipe --------------------
# VirtualGL's libvglfaker.so leaves eglGetProcAddress / glXGetProcAddressARB
# undefined (resolved from already-loaded libEGL/libGL). Preload those with
# RTLD_GLOBAL semantics via LD_PRELOAD so vglrun -d egl can start.
CHUDWARE_PREFIX=()
USE_VGL=0
HAS_NVIDIA=0
if [ -e /dev/nvidia0 ] || [ -e /dev/nvidiactl ]; then
    HAS_NVIDIA=1
fi

enable_llvmpipe() {
    unset LD_PRELOAD 2>/dev/null || true
    # Drop any leftover VGL preloads; force software Mesa.
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
    unset VGL_DISPLAY 2>/dev/null || true
    CHUDWARE_PREFIX=()
    USE_VGL=0
}

if [ "$CHUDWARE_HEAVY" = "1" ] && [ "$HAS_NVIDIA" = "1" ]; then
    if command -v vglrun >/dev/null 2>&1; then
        log "CHUDWARE_HEAVY=1 + NVIDIA — using VirtualGL (vglrun -d egl)"
        export VGL_DISPLAY="${VGL_DISPLAY:-egl}"
        export VGL_LOGO=0
        # Preload dispatch + EGL + GL so libvglfaker resolves GetProcAddress symbols.
        _vgl_preload=""
        for _lib in \
            /usr/lib64/libGLdispatch.so.0 \
            /usr/lib64/libOpenGL.so.0 \
            /usr/lib64/libEGL.so.1 \
            /usr/lib64/libGL.so.1
        do
            if [ -e "$_lib" ]; then
                _vgl_preload="${_vgl_preload:+$_vgl_preload:}$_lib"
            fi
        done
        if [ -n "$_vgl_preload" ]; then
            export LD_PRELOAD="${_vgl_preload}${LD_PRELOAD:+:$LD_PRELOAD}"
            log "VGL LD_PRELOAD=$LD_PRELOAD"
        fi
        # Don't force software GL under VGL.
        unset LIBGL_ALWAYS_SOFTWARE 2>/dev/null || true
        unset GALLIUM_DRIVER 2>/dev/null || true
        CHUDWARE_PREFIX=(vglrun -d egl)
        USE_VGL=1
    else
        log "WARNING: CHUDWARE_HEAVY=1 but VirtualGL/vglrun missing — falling back to llvmpipe"
        enable_llvmpipe
    fi
else
    if [ "$CHUDWARE_HEAVY" = "1" ] && [ "$HAS_NVIDIA" != "1" ]; then
        log "CHUDWARE_HEAVY=1 but no NVIDIA devices — llvmpipe"
    fi
    enable_llvmpipe
fi

# ---- Stream backend first (so /health of stream is up while Chudware boots) ----
start_novnc() {
    log "STREAM_BACKEND=novnc — x11vnc :$VNC_PORT + websockify :$STREAM_PORT"
    # -forever -shared: reconnect-friendly; -nopw: gateway terminates auth
    x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -localhost -nopw \
        -forever -shared -noxdamage -wait 10 -defer 10 \
        -o /tmp/x11vnc.log &
    PIDS+=($!)
    sleep 0.5

    # Fedora: websockify binary + /usr/share/novnc web root
    local web="/usr/share/novnc"
    if [ ! -d "$web" ]; then
        log "FATAL: /usr/share/novnc missing"
        exit 1
    fi
    # Prefer novnc_proxy if present (sets up sensible defaults), else raw websockify
    if command -v novnc_proxy >/dev/null 2>&1; then
        novnc_proxy --listen "$STREAM_PORT" --vnc "127.0.0.1:$VNC_PORT" --web "$web" &
        PIDS+=($!)
    else
        websockify --web="$web" "$STREAM_PORT" "127.0.0.1:$VNC_PORT" &
        PIDS+=($!)
    fi
    log "noVNC listening on 0.0.0.0:$STREAM_PORT (vnc 127.0.0.1:$VNC_PORT)"
}

start_selkies() {
    log "STREAM_BACKEND=selkies (experimental on Fedora)"
    if ! command -v selkies-gstreamer >/dev/null 2>&1 && ! python3 -c 'import selkies_gstreamer' 2>/dev/null; then
        log "WARNING: selkies not available — falling back to novnc"
        STREAM_BACKEND=novnc
        start_novnc
        return
    fi
    # Legacy PyPI selkies 1.6.1 is GStreamer-based (selkies-gstreamer).
    # Modern pixelflux stack is NOT wired here; novnc is the supported default.
    export SELKIES_LISTEN_PORT="${SELKIES_LISTEN_PORT:-$STREAM_PORT}"
    export SELKIES_ENCODER
    export DISPLAY
    if command -v selkies-gstreamer >/dev/null 2>&1; then
        selkies-gstreamer --addr=0.0.0.0 --port="$STREAM_PORT" \
            --encoder="${SELKIES_ENCODER}" \
            --enable_basic_auth=false \
            2>/tmp/selkies.log &
        PIDS+=($!)
    else
        python3 -m selkies_gstreamer --addr=0.0.0.0 --port="$STREAM_PORT" \
            --encoder="${SELKIES_ENCODER}" \
            --enable_basic_auth=false \
            2>/tmp/selkies.log &
        PIDS+=($!)
    fi
    log "selkies started on :$STREAM_PORT (encoder=$SELKIES_ENCODER)"
}

case "$STREAM_BACKEND" in
    selkies) start_selkies ;;
    novnc|*) start_novnc ;;
esac

# ---- Chudware -----------------------------------------------------------------
CHUDWARE_BIN="$CHUDWARE_ROOT/bin/Chudware"
if [ ! -x "$CHUDWARE_BIN" ]; then
    log "FATAL: $CHUDWARE_BIN not executable"
    exit 1
fi
# File dialogs / Feature-Manager explorer start in the shared projects tree.
export CHUDWARE_START_DIR="${CHUDWARE_START_DIR:-$HOME/Projects}"

# Default project: open its work-in-progress document at startup (bare path
# argv = the app's file-association open path). Pull-only PLM bootstrap above
# has already cloned /plm/*.git into $HOME/Projects/. "WIP" = the most
# recently modified .step+ in the project.
CHUDWARE_OPEN_ARGS=()
if [ -n "${CHUDWARE_DEFAULT_PROJECT:-}" ]; then
    proj_dir="$HOME/Projects/${CHUDWARE_DEFAULT_PROJECT}"
    if [ -d "$proj_dir" ]; then
        doc=""
        if [ -n "${CHUDWARE_OPEN_DOC:-}" ] && [ -f "$proj_dir/$CHUDWARE_OPEN_DOC" ]; then
            doc="$proj_dir/$CHUDWARE_OPEN_DOC"
        else
            [ -n "${CHUDWARE_OPEN_DOC:-}" ] && log "WARN CHUDWARE_OPEN_DOC not found: $proj_dir/$CHUDWARE_OPEN_DOC"
            doc="$(find "$proj_dir" -maxdepth 2 -name '*.step+' -type f -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2-)"
        fi
        if [ -n "$doc" ]; then
            CHUDWARE_OPEN_ARGS=("$doc")
            log "default project '$CHUDWARE_DEFAULT_PROJECT' — opening $(basename "$doc")"
        else
            log "default project '$CHUDWARE_DEFAULT_PROJECT' has no .step+ document yet"
        fi
    else
        log "WARN default project dir missing: $proj_dir"
    fi
fi

launch_billet() {
    log "launching Chudware as ${CHUDWARE_USER_EMAIL:-?} / ${CHUDWARE_USER_DISPLAY}"
    log "  DISPLAY=$DISPLAY STREAM_BACKEND=$STREAM_BACKEND HEAVY=$CHUDWARE_HEAVY USE_VGL=$USE_VGL"
    log "  prefix=${CHUDWARE_PREFIX[*]:-(none)}"
    if [ ${#CHUDWARE_PREFIX[@]} -gt 0 ]; then
        "${CHUDWARE_PREFIX[@]}" "$CHUDWARE_BIN" "${CHUDWARE_OPEN_ARGS[@]}" &
    else
        "$CHUDWARE_BIN" "${CHUDWARE_OPEN_ARGS[@]}" &
    fi
    CHUDWARE_PID=$!
    # Replace any previous Chudware pid in PIDS with the live one.
    local cleaned=()
    local p
    for p in "${PIDS[@]:-}"; do
        [ "$p" = "${PREV_CHUDWARE_PID:-}" ] && continue
        cleaned+=("$p")
    done
    PIDS=("${cleaned[@]}")
    PIDS+=("$CHUDWARE_PID")
    PREV_CHUDWARE_PID=$CHUDWARE_PID
    log "Chudware pid=$CHUDWARE_PID"
}

launch_billet

# If VirtualGL fails (missing EGL symbols, bad CDI libs, …) Chudware dies immediately.
# Detect early exit and relaunch under llvmpipe so the seat stays usable.
if [ "$USE_VGL" = "1" ]; then
    set +e
    for _i in 1 2 3 4 5 6 7 8; do
        if ! kill -0 "$CHUDWARE_PID" 2>/dev/null; then
            wait "$CHUDWARE_PID" 2>/dev/null
            _rc=$?
            log "WARNING: Chudware under VirtualGL exited within ${_i}s (rc=${_rc}) — falling back to llvmpipe"
            enable_llvmpipe
            launch_billet
            break
        fi
        sleep 1
    done
    set -e
fi

# Wait on Chudware; if it dies later, tear everything down.
set +e
wait "$CHUDWARE_PID"
rc=$?
set -e
log "Chudware exited rc=$rc"
exit "$rc"
