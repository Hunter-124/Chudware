#!/usr/bin/env bash
# Mega-22 gate-gated launcher. Do not invoke until both IRC signals received;
# this is a local safety recheck, not the IRC gate itself.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
OUT=reports/runs/mega-22
PY="${HOME}/.local/share/AI-CAD/mcp-venv/bin/python"

# Require fixes.md
if [[ ! -f "$OUT/fixes.md" ]]; then
  echo "[gate] missing $OUT/fixes.md — Fixer22 not bound"
  exit 10
fi
if ! grep -qiE 'bound|P22|Q22|R22|M21|N21' "$OUT/fixes.md"; then
  echo "[gate] fixes.md present but no P22/Q22/R22/bound marker"
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
Path("reports/runs/mega-22/daemon_preflight.json").write_text(json.dumps(out, indent=2, default=str))
print(json.dumps({k: out[k] for k in out if k != "server_info"}, indent=2))
if run != disk:
    print("[gate] daemon git_rev != disk binary", run, disk)
    sys.exit(12)
if info.get("state") not in (None, "ready", "idle"):
    # ready is expected
    pass
if info.get("state") and info.get("state") not in ("ready",):
    print("[gate] daemon state not ready:", info.get("state"))
    # still allow if matches
PY2

# No other xai mega consumer (except us)
if pgrep -af 'run_mega' | grep -v mega-22 | grep -E 'xai|mega-1[0-9]|mega-2[01]' >/dev/null 2>&1; then
  echo "[gate] other mega/xai consumer live:"
  pgrep -af 'run_mega' || true
  exit 13
fi

echo "[gate] OK — running preflight then primary"
"$PY" "$OUT/_preflight.py" 2>&1 | tee "$OUT/preflight_log.txt"
# require preflight_ready
"$PY" - <<'PY3'
import json, sys
from pathlib import Path
s = json.loads(Path("reports/runs/mega-22/preflight_summary.json").read_text())
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
