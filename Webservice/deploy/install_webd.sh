#!/usr/bin/env bash
# ===========================================================================
#  install_webd.sh — promote a green engine build to the SHARED web daemon.
# ---------------------------------------------------------------------------
#  Installs a pinned copy of chudware-hostd + libchudware.so (and its $ORIGIN
#  sibling .so deps) into ~/chudware-web/bin, separate from dev-tree builds, and
#  wires the systemd user unit (chudware-hostd.service). The daemon is namespaced
#  by CHUDWARE_INSTANCE_KEY=chudwareweb so it NEVER shares a socket with a dev
#  daemon (dev keys derive from the binary realpath).
#
#  Upgrades: re-run this script. Files are staged copy-to-temp + atomic mv
#  (never cp over a live mmap'ed .so — SIGBUS; see docs/HOSTD.md "Upgrades"),
#  then the unit restarts on demand:
#    ./install_webd.sh            install/refresh files + unit (no restart)
#    ./install_webd.sh --restart  also restart chudware-hostd.service
#
#  OCCT (libTK*.so) resolves from the system ldconfig path — this is a
#  single-host deployment (same machine that built it). The script verifies
#  the installed daemon runs before touching the unit.
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web}"
BIN="$DST/bin"
KEY="chudwareweb"
WORKSPACE_DEFAULT="$HOME/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"

# Always land a real directory: gateway /workspace/* 503s when the root is missing
# (common after host migration before CAD_Build is restored). Prefer an existing
# env override; otherwise create the ME489 default so a fresh box still serves.
WORKSPACE_ROOT="${CHUDWARE_WORKSPACE_ROOT:-$WORKSPACE_DEFAULT}"
if [ ! -d "$WORKSPACE_ROOT" ]; then
    mkdir -p "$WORKSPACE_ROOT"
    echo "[webd] created workspace root $WORKSPACE_ROOT"
fi
RESTART=0
[ "${1:-}" = "--restart" ] && RESTART=1

# --- pick the source build tree (full OCCT engine preferred) ---------------
SRC=""
if [ -f "$ROOT/core/build-occt-linux/bin/libchudware.so" ]; then
    SRC="$ROOT/core/build-occt-linux/bin"
elif [ -f "$ROOT/services/dist/Chudware/libchudware.so" ]; then
    SRC="$ROOT/services/dist/Chudware"
    echo "[webd] NOTE: installing from services/dist/Chudware (staged) — ok if staged from occt tree"
elif [ -f "$ROOT/core/build-linux/bin/libchudware.so" ]; then
    SRC="$ROOT/core/build-linux/bin"
    echo "[webd] WARNING: slim engine (no OCCT/Netgen) — FEA/CFD RPCs unavailable"
else
    echo "[webd] ERROR: no built libchudware.so (run ./build.sh core / configure core/build-occt-linux)"
    exit 1
fi
[ -f "$SRC/chudware-hostd" ] || { echo "[webd] ERROR: $SRC/chudware-hostd missing (binaries MUST come from one tree)"; exit 1; }

mkdir -p "$BIN"

# --- atomic install of the engine set (same set build.sh stage deploys) ----
staged=0
for f in libchudware.so libngcore.so libnglib.so lib3mf.so chudware-hostd; do
    [ -e "$SRC/$f" ] || continue
    tmp="$BIN/.$f.staging.$$"
    cp "$SRC/$f" "$tmp"
    mv -f "$tmp" "$BIN/$f"
    staged=$((staged + 1))
    echo "[webd] $f -> $BIN/"
done
echo "[webd] installed $staged file(s) from $SRC"

# --- sanity: pinned daemon must at least answer --version -------------------
"$BIN/chudware-hostd" --version >/dev/null || { echo "[webd] ERROR: installed daemon won't run"; exit 1; }

# --- agent role prompts (agent_run_v2 reads these; env-pinned in the unit) ---
# The daemon has no WorkingDirectory, so the relative "agent/prompts" fallback in
# roles.cpp resolves against $HOME/'/' and every agent run throws "role prompt
# unreadable/empty". Deploy the tree next to the daemon and point CHUDWARE_PROMPTS_DIR
# at it (stamped into the unit below). Text files, re-read per run — a plain tree
# swap is safe (no live-mmap SIGBUS hazard like the .so files).
PROMPTS_SRC="$ROOT/agent/prompts"
PROMPTS_DST="$DST/agent/prompts"
if [ -d "$PROMPTS_SRC" ]; then
  mkdir -p "$DST/agent"
  tmp_p="$DST/agent/.prompts.tmp.$$"
  rm -rf "$tmp_p"
  cp -r "$PROMPTS_SRC" "$tmp_p"
  rm -rf "$PROMPTS_DST"
  mv "$tmp_p" "$PROMPTS_DST"
  echo "[webd] deployed agent role prompts -> $PROMPTS_DST ($(find "$PROMPTS_DST" -name '*.md' | wc -l) md files)"
else
  echo "[webd] WARNING: $PROMPTS_SRC missing — agent_run_v2 will fail (role prompts)"
fi

# --- socket path (verbatim instance-key override => deterministic name) ----
RUNDIR="${XDG_RUNTIME_DIR:-/tmp/chudware-$(id -u)}"
SOCK="$RUNDIR/chudware/hostd-$KEY.sock"

# --- env file the gateway reads (CHUDWARE_WEBD_* defaults) --------------------
cat > "$DST/webd.env" <<EOF
# written by install_webd.sh $(date -Is) — read by the gateway (config.py)
CHUDWARE_WEBD_SOCKET=$SOCK
CHUDWARE_WORKSPACE_ROOT=$WORKSPACE_ROOT
EOF
echo "[webd] wrote $DST/webd.env (socket $SOCK, workspace $WORKSPACE_ROOT)"

# --- launcher for Hunter's local GUI to join the shared session -------------
cat > "$DST/chudware-shared.sh" <<EOF
#!/usr/bin/env bash
# Launch the desktop Chudware attached to the SHARED web daemon (chudware-webd).
# A client-side spawn (daemon not running) resurrects the PINNED binary, not a
# dev build.
export CHUDWARE_INSTANCE_KEY=$KEY
export CHUDWARE_HOSTD_BIN="$BIN/chudware-hostd"
export CHUDWARE_ENGINE_DAEMON=1
exec "\${CHUDWARE_BIN:-$ROOT/host/build-linux/bin/Chudware}" "\$@"
EOF
chmod +x "$DST/chudware-shared.sh"
echo "[webd] wrote $DST/chudware-shared.sh"

# --- systemd user unit -------------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s|@BIN@|$BIN|g" -e "s|@PROMPTS@|$PROMPTS_DST|g" \
  "$ROOT/Webservice/deploy/chudware-hostd.service" > "$UNIT_DIR/chudware-hostd.service"
# Auto-reinstall path unit: watches the build-tree chudware-hostd and re-runs
# this script --restart. Enabling is idempotent; path watches the SOURCE
# binary (not $BIN), so the install itself cannot re-trigger the path unit.
cp "$ROOT/Webservice/deploy/chudware-webd-sync.path" \
   "$ROOT/Webservice/deploy/chudware-webd-sync.service" \
   "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable chudware-hostd.service >/dev/null 2>&1 || true
systemctl --user enable --now chudware-webd-sync.path >/dev/null 2>&1 || true
echo "[webd] unit installed: chudware-hostd.service"
echo "[webd] unit installed: chudware-webd-sync.path (+ .service)"

if [ "$RESTART" = 1 ]; then
    # The chudware-webd-sync.path unit fires this on EVERY dev/harness rebuild of
    # chudware-hostd. A plain `systemctl restart` here tears down every connected web
    # user's engine bridge at once — the dominant cause of the "web service is very
    # unstable" reports (10+ restarts in a 2h dev window). Two guards make the shared
    # daemon stop flapping under active development:
    #   1. sha-skip: if the staged binary is byte-identical to what we last restarted
    #      onto (burst builds re-trigger the path unit; a manual re-run), do nothing.
    #   2. idle-wait: while the gateway reports live engine bridges, DEFER the restart
    #      (poll /healthz) up to CHUDWARE_WEBD_RESTART_MAX_WAIT so the swap lands in a
    #      natural lull instead of mid-edit. The sync .service TimeoutStartSec covers
    #      this wait. Override the URL/cap via env for dev gateways.
    STAMP="$BIN/.webd_running_sha"
    NEW_SHA="$(sha256sum "$BIN/chudware-hostd" | awk '{print $1}')"
    RUNNING=0
    systemctl --user is-active --quiet chudware-hostd.service && RUNNING=1
    if [ "$RUNNING" = 1 ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$NEW_SHA" ]; then
        echo "[webd] staged binary unchanged (sha ${NEW_SHA:0:12}) and daemon live — no restart"
    else
        GW="${CHUDWARE_WEB_GATEWAY_URL:-http://127.0.0.1:8300}"
        MAXW="${CHUDWARE_WEBD_RESTART_MAX_WAIT:-600}"
        if [ "$RUNNING" = 1 ]; then
            waited=0
            while [ "$waited" -lt "$MAXW" ]; do
                n="$(curl -fs --max-time 3 "$GW/healthz" 2>/dev/null \
                        | sed -n 's/.*"engine_bridges"[: ]*\([0-9]*\).*/\1/p')"
                # Empty => gateway unreachable/old (no one can be connected) — restart now.
                [ -z "$n" ] && break
                [ "$n" = "0" ] && break
                echo "[webd] $n web session(s) active — deferring restart (${waited}s/${MAXW}s)"
                sleep 5; waited=$((waited + 5))
            done
        fi
        systemctl --user restart chudware-hostd.service
        sleep 1
        CHUDWARE_INSTANCE_KEY=$KEY "$BIN/chudware-hostd" --status || {
            echo "[webd] ERROR: daemon failed to come up"; exit 1; }
        echo "$NEW_SHA" > "$STAMP"
        echo "[webd] restarted chudware-hostd (sha ${NEW_SHA:0:12})"
    fi
else
    echo "[webd] start it with: systemctl --user start chudware-hostd.service"
fi
