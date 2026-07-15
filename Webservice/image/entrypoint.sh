#!/usr/bin/env bash
# entrypoint.sh — Xvfb + Billet + stream backend (novnc default, selkies optional)
set -euo pipefail

log() { echo "[billet-session $(date -u +%H:%M:%S)] $*" >&2; }

# ---- Contract defaults ------------------------------------------------------
export DISPLAY="${DISPLAY:-:0}"
export HOME="${HOME:-/home/user}"
export BILLET_PLM_DIR="${BILLET_PLM_DIR:-/plm}"
export STREAM_BACKEND="${STREAM_BACKEND:-novnc}"   # novnc | selkies
export SELKIES_ENCODER="${SELKIES_ENCODER:-x264enc}"
export BILLET_HEAVY="${BILLET_HEAVY:-0}"
export AI_CAD_USER_ID="${AICAD_USER_ID:-${BILLET_USER_EMAIL:-user}}"
export AICAD_USER_ID="${AICAD_USER_ID:-$AI_CAD_USER_ID}"
export AI_CAD_USER_DISPLAY="${AICAD_USER_DISPLAY:-${BILLET_USER_DISPLAY:-User}}"
export AICAD_USER_DISPLAY="${AICAD_USER_DISPLAY:-$AI_CAD_USER_DISPLAY}"

# CEF: compile-time linked; lazy cold-init reduces container start risk.
# no_sandbox already set in cef_platform_linux.cpp; single-process default on.
export AI_CAD_CEF_LAZY="${AI_CAD_CEF_LAZY:-1}"
export AI_CAD_CEF_TRACE="${AI_CAD_CEF_TRACE:-0}"

BILLET_ROOT="${BILLET_ROOT:-/opt/billet}"
export PATH="$BILLET_ROOT/bin:$PATH"
export LD_LIBRARY_PATH="$BILLET_ROOT/lib:$BILLET_ROOT/bin:${LD_LIBRARY_PATH:-}"
# Explicit path so cpp_core ensureLoaded finds the engine even if dlopen
# candidates miss (see services/worker/cpp_core.cpp).
export AI_CAD_LIBAICAD="${AI_CAD_LIBAICAD:-$BILLET_ROOT/bin/libaicad.so}"

# Screen geometry — matches Xvfb; GLFW window is 1440x900 by default (app.cpp).
SCREEN_W="${SCREEN_W:-1920}"
SCREEN_H="${SCREEN_H:-1080}"
SCREEN_D="${SCREEN_D:-24}"
STREAM_PORT="${STREAM_PORT:-8080}"
VNC_PORT="${VNC_PORT:-5900}"

# HOME may be a bind-mount; don't fail the seat if mkdir races or perms lag.
mkdir -p "$HOME" 2>/dev/null || true
mkdir -p "$HOME/.local/share" 2>/dev/null || true
mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
cd "$HOME" 2>/dev/null || cd /

PIDS=()
cleanup() {
    log "SIGTERM/EXIT — shutting down (${#PIDS[@]} children)"
    # Reverse order: streamer, billet, vnc, xvfb
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
BILLET_PREFIX=()
HAS_NVIDIA=0
if [ -e /dev/nvidia0 ] || [ -e /dev/nvidiactl ] || ls /dev/dri/card* >/dev/null 2>&1; then
    # nvidia device nodes present via CDI
    if [ -e /dev/nvidia0 ] || [ -e /dev/nvidiactl ]; then
        HAS_NVIDIA=1
    fi
fi

if [ "$BILLET_HEAVY" = "1" ] && [ "$HAS_NVIDIA" = "1" ]; then
    if command -v vglrun >/dev/null 2>&1; then
        log "BILLET_HEAVY=1 + NVIDIA — using VirtualGL (vglrun -d egl)"
        export VGL_DISPLAY="${VGL_DISPLAY:-egl}"
        export VGL_LOGO=0
        BILLET_PREFIX=(vglrun -d egl)
    else
        log "WARNING: BILLET_HEAVY=1 but VirtualGL/vglrun missing — falling back to llvmpipe"
    fi
else
    if [ "$BILLET_HEAVY" = "1" ] && [ "$HAS_NVIDIA" != "1" ]; then
        log "BILLET_HEAVY=1 but no NVIDIA devices — llvmpipe"
    fi
    # Force software GL when not using VirtualGL so Mesa doesn't probe missing DRM.
    export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
    export GALLIUM_DRIVER="${GALLIUM_DRIVER:-llvmpipe}"
fi

# ---- Stream backend first (so /health of stream is up while Billet boots) ----
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

# ---- Billet -----------------------------------------------------------------
BILLET_BIN="$BILLET_ROOT/bin/Billet"
if [ ! -x "$BILLET_BIN" ]; then
    log "FATAL: $BILLET_BIN not executable"
    exit 1
fi

log "launching Billet as ${BILLET_USER_EMAIL:-?} / ${AICAD_USER_DISPLAY}"
log "  DISPLAY=$DISPLAY STREAM_BACKEND=$STREAM_BACKEND HEAVY=$BILLET_HEAVY"
log "  prefix=${BILLET_PREFIX[*]:-(none)}"

# Run under the session user identity (container USER is already 'user').
"${BILLET_PREFIX[@]}" "$BILLET_BIN" &
BILLET_PID=$!
PIDS+=($BILLET_PID)
log "Billet pid=$BILLET_PID"

# Wait on Billet; if it dies, tear everything down.
set +e
wait "$BILLET_PID"
rc=$?
set -e
log "Billet exited rc=$rc"
exit "$rc"
