#!/usr/bin/env bash
# ===========================================================================
#  install_webd.sh — promote a green engine build to the SHARED web daemon.
# ---------------------------------------------------------------------------
#  Installs a pinned copy of aicad-hostd + libaicad.so (and its $ORIGIN
#  sibling .so deps) into ~/billet-web/bin, separate from dev-tree builds, and
#  wires the systemd user unit (billet-hostd.service). The daemon is namespaced
#  by AICAD_INSTANCE_KEY=billetweb so it NEVER shares a socket with a dev
#  daemon (dev keys derive from the binary realpath).
#
#  Upgrades: re-run this script. Files are staged copy-to-temp + atomic mv
#  (never cp over a live mmap'ed .so — SIGBUS; see docs/HOSTD.md "Upgrades"),
#  then the unit restarts on demand:
#    ./install_webd.sh            install/refresh files + unit (no restart)
#    ./install_webd.sh --restart  also restart billet-hostd.service
#
#  OCCT (libTK*.so) resolves from the system ldconfig path — this is a
#  single-host deployment (same machine that built it). The script verifies
#  the installed daemon runs before touching the unit.
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DST="${BILLET_WEB_HOME:-$HOME/billet-web}"
BIN="$DST/bin"
KEY="billetweb"
WORKSPACE_DEFAULT="$HOME/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"
RESTART=0
[ "${1:-}" = "--restart" ] && RESTART=1

# --- pick the source build tree (full OCCT engine preferred) ---------------
SRC=""
if [ -f "$ROOT/core/build-occt-linux/bin/libaicad.so" ]; then
    SRC="$ROOT/core/build-occt-linux/bin"
elif [ -f "$ROOT/services/dist/AI-CAD/libaicad.so" ]; then
    SRC="$ROOT/services/dist/AI-CAD"
    echo "[webd] NOTE: installing from services/dist/AI-CAD (staged) — ok if staged from occt tree"
elif [ -f "$ROOT/core/build-linux/bin/libaicad.so" ]; then
    SRC="$ROOT/core/build-linux/bin"
    echo "[webd] WARNING: slim engine (no OCCT/Netgen) — FEA/CFD RPCs unavailable"
else
    echo "[webd] ERROR: no built libaicad.so (run ./build.sh core / configure core/build-occt-linux)"
    exit 1
fi
[ -f "$SRC/aicad-hostd" ] || { echo "[webd] ERROR: $SRC/aicad-hostd missing (binaries MUST come from one tree)"; exit 1; }

mkdir -p "$BIN"

# --- atomic install of the engine set (same set build.sh stage deploys) ----
staged=0
for f in libaicad.so libngcore.so libnglib.so lib3mf.so aicad-hostd; do
    [ -e "$SRC/$f" ] || continue
    tmp="$BIN/.$f.staging.$$"
    cp "$SRC/$f" "$tmp"
    mv -f "$tmp" "$BIN/$f"
    staged=$((staged + 1))
    echo "[webd] $f -> $BIN/"
done
echo "[webd] installed $staged file(s) from $SRC"

# --- sanity: pinned daemon must at least answer --version -------------------
"$BIN/aicad-hostd" --version >/dev/null || { echo "[webd] ERROR: installed daemon won't run"; exit 1; }

# --- agent role prompts (agent_run_v2 reads these; env-pinned in the unit) ---
# The daemon has no WorkingDirectory, so the relative "agent/prompts" fallback in
# roles.cpp resolves against $HOME/'/' and every agent run throws "role prompt
# unreadable/empty". Deploy the tree next to the daemon and point AI_CAD_PROMPTS_DIR
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
RUNDIR="${XDG_RUNTIME_DIR:-/tmp/aicad-$(id -u)}"
SOCK="$RUNDIR/aicad/hostd-$KEY.sock"

# --- env file the gateway reads (BILLET_WEBD_* defaults) --------------------
cat > "$DST/webd.env" <<EOF
# written by install_webd.sh $(date -Is) — read by the gateway (config.py)
BILLET_WEBD_SOCKET=$SOCK
BILLET_WORKSPACE_ROOT=${BILLET_WORKSPACE_ROOT:-$WORKSPACE_DEFAULT}
EOF
echo "[webd] wrote $DST/webd.env (socket $SOCK)"

# --- launcher for Hunter's local GUI to join the shared session -------------
cat > "$DST/billet-shared.sh" <<EOF
#!/usr/bin/env bash
# Launch the desktop Billet attached to the SHARED web daemon (billet-webd).
# A client-side spawn (daemon not running) resurrects the PINNED binary, not a
# dev build.
export AICAD_INSTANCE_KEY=$KEY
export AICAD_HOSTD_BIN="$BIN/aicad-hostd"
export AI_CAD_ENGINE_DAEMON=1
exec "\${BILLET_BIN:-$ROOT/host/build-linux/bin/Billet}" "\$@"
EOF
chmod +x "$DST/billet-shared.sh"
echo "[webd] wrote $DST/billet-shared.sh"

# --- systemd user unit -------------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s|@BIN@|$BIN|g" -e "s|@PROMPTS@|$PROMPTS_DST|g" \
  "$ROOT/Webservice/deploy/billet-hostd.service" > "$UNIT_DIR/billet-hostd.service"
# Auto-reinstall path unit: watches the build-tree aicad-hostd and re-runs
# this script --restart. Enabling is idempotent; path watches the SOURCE
# binary (not $BIN), so the install itself cannot re-trigger the path unit.
cp "$ROOT/Webservice/deploy/billet-webd-sync.path" \
   "$ROOT/Webservice/deploy/billet-webd-sync.service" \
   "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable billet-hostd.service >/dev/null 2>&1 || true
systemctl --user enable --now billet-webd-sync.path >/dev/null 2>&1 || true
echo "[webd] unit installed: billet-hostd.service"
echo "[webd] unit installed: billet-webd-sync.path (+ .service)"

if [ "$RESTART" = 1 ]; then
    systemctl --user restart billet-hostd.service
    sleep 1
    AICAD_INSTANCE_KEY=$KEY "$BIN/aicad-hostd" --status || {
        echo "[webd] ERROR: daemon failed to come up"; exit 1; }
else
    echo "[webd] start it with: systemctl --user start billet-hostd.service"
fi
