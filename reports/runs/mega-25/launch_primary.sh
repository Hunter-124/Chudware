#!/usr/bin/env bash
# Mega-24 primary launch (after Fixer25 fixes bound). Liveness watch co-started.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
OUT=reports/runs/mega-25
PY="${HOME}/.local/share/AI-CAD/mcp-venv/bin/python"

if [[ ! -f "$OUT/fixes.md" ]]; then
  echo "[gate] missing $OUT/fixes.md — Fixer25 not bound"
  exit 10
fi
if ! grep -qiE 'bound|Y25|Z25|fixes bound' "$OUT/fixes.md"; then
  echo "[gate] fixes.md present but no bound/U24/V24 marker"
  exit 11
fi

# re-check daemon
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
  "state": info.get("state"),
}
Path("reports/runs/mega-25/daemon_preflight.json").write_text(json.dumps(out, indent=2, default=str))
print(json.dumps(out, indent=2))
if run != disk:
    print("[gate] daemon git_rev != disk binary", run, disk)
    sys.exit(12)
PY2

# no other xai consumer
if pgrep -af 'run_mega' | grep -v mega-25 | grep -E 'xai|mega-' >/dev/null 2>&1; then
  echo "[gate] other mega/xai consumer live:"
  pgrep -af 'run_mega' || true
  exit 13
fi

# re-run preflight
"$PY" "$OUT/_preflight.py" 2>&1 | tee "$OUT/preflight_log.txt"
"$PY" - <<'PY3'
import json, sys
from pathlib import Path
s = json.loads(Path("reports/runs/mega-25/preflight_summary.json").read_text())
print("preflight_ready", s.get("preflight_ready_for_launch"))
if not s.get("preflight_ready_for_launch"):
    sys.exit(14)
if s.get("no_other_xai_consumer") is False:
    print("other xai", s.get("other_xai_consumers"))
    sys.exit(15)
PY3

# clear prior events/status if any unfinished
rm -f "$OUT/events.jsonl" "$OUT/primary.log" "$OUT/liveness_kill.json" "$OUT/DM_FIXER25.txt"

nohup "$PY" "$OUT/run_mega.py" > "$OUT/primary.log" 2>&1 &
echo $! > "$OUT/primary.pid"
echo "[launch] primary pid=$(cat $OUT/primary.pid) log=$OUT/primary.log"

nohup "$PY" "$OUT/liveness_watch.py" --pid "$(cat $OUT/primary.pid)" > "$OUT/liveness.log" 2>&1 &
echo $! > "$OUT/liveness.pid"
echo "[launch] liveness pid=$(cat $OUT/liveness.pid) log=$OUT/liveness.log"
