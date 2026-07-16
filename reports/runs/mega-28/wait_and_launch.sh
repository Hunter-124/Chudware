#!/usr/bin/env bash
# Mega-28 gate-gated launcher. Do not invoke until both IRC signals received;
# this is a local safety recheck, not the IRC gate itself.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
OUT=reports/runs/mega-28
PY="${HOME}/.local/share/AI-CAD/mcp-venv/bin/python"

# Require fixes.md
if [[ ! -f "$OUT/fixes.md" ]]; then
  echo "[gate] missing $OUT/fixes.md — RouterFix not bound"
  exit 10
fi
if ! grep -qiE 'bound|E28|F28|C27|D27|router proven|bound|fixes bound' "$OUT/fixes.md"; then
  echo "[gate] fixes.md present but no bound/E28/router proven marker"
  exit 11
fi

# Daemon match rebuilt binary
"$PY" - <<'PY2'
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, ".")
from mcp_server.daemon_bridge import DaemonClient
c = DaemonClient()
info = c.call("hello")
ver = subprocess.check_output(["core/build-occt-linux/bin/aicad-hostd", "--version"], text=True).strip()
disk = ver.split()[-1]
run = info.get("git_rev") or info.get("engine_build_id")
out = {
  "running_git_rev": run,
  "disk_build_id": disk,
  "disk_version_line": ver,
  "running_matches_disk_binary": run == disk,
  "daemon_pid": info.get("daemon_pid"),
  "server_info": info,
  "state": info.get("state"),
}
Path("reports/runs/mega-28/daemon_preflight.json").write_text(json.dumps(out, indent=2, default=str))
print(json.dumps({k: out[k] for k in out if k != "server_info"}, indent=2))
if run != disk:
    print("[gate] daemon git_rev != disk binary", run, disk)
    sys.exit(12)
baseline_stamp = "night2-3077-g0f43d126-dirty"  # mega-27 a1 daemon
stamp_path = Path("reports/runs/mega-28/expected_daemon_stamp.txt")
if stamp_path.exists():
    expected = stamp_path.read_text(encoding="utf-8").strip().split()[-1]
    if run != expected and disk != expected:
        print("[gate] daemon stamp != expected NEW RouterFix stamp", run, disk, expected)
        sys.exit(16)
    print("[gate] daemon matches expected RouterFix stamp", expected)
elif run == baseline_stamp or disk == baseline_stamp:
    print("[gate] daemon still on mega-27 baseline stamp; wait for RouterFix rebuild", run, disk)
    sys.exit(17)
print("[gate] daemon stamp OK (post-RouterFix)", run)
if info.get("state") and info.get("state") not in ("ready", None, "idle"):
    print("[gate] daemon state not ready:", info.get("state"))
PY2

# No other xai mega consumer (except us)
if pgrep -af '[r]un_mega\.py' | grep -v 'mega-28' >/dev/null 2>&1; then
  echo "[gate] other mega/xai consumer live:"
  pgrep -af '[r]un_mega\.py' || true
  exit 13
fi

echo "[gate] OK — running preflight then primary"
"$PY" "$OUT/_preflight.py" 2>&1 | tee "$OUT/preflight_log.txt"
# require preflight_ready
"$PY" - <<'PY3'
import json, sys
from pathlib import Path
s = json.loads(Path("reports/runs/mega-28/preflight_summary.json").read_text())
print("preflight_ready", s.get("preflight_ready_for_launch"))
if not s.get("preflight_ready_for_launch"):
    sys.exit(14)
# also require no other xai if field present
if s.get("no_other_xai_consumer") is False:
    print("other xai", s.get("other_xai_consumers"))
    sys.exit(15)
PY3

nohup "$PY" "$OUT/run_mega.py" > "$OUT/primary.log" 2>&1 &
echo $! > "$OUT/primary.pid"
echo "[launch] primary pid=$(cat $OUT/primary.pid) log=$OUT/primary.log"

# liveness watch (kept from mega-27)
nohup "$PY" "$OUT/liveness_watch.py" --pid "$(cat $OUT/primary.pid)" > "$OUT/liveness.log" 2>&1 &
echo $! > "$OUT/liveness.pid"
echo "[launch] liveness pid=$(cat $OUT/liveness.pid) log=$OUT/liveness.log"
