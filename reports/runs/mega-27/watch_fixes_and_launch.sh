#!/usr/bin/env bash
# Wait for Fixer27 fixes.md 'fixes bound' then launch primary with liveness.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
OUT=reports/runs/mega-27
PY="${HOME}/.local/share/AI-CAD/mcp-venv/bin/python"
MARKER_RE='fixes bound|C27.*bound|D27.*bound|A26.*bound|B26.*bound|BOUND'

echo "[watch] waiting for $OUT/fixes.md with bound marker (Fixer27=AshamedLungfish)"
for i in $(seq 1 720); do  # up to ~60 min @5s
  if [[ -f "$OUT/fixes.md" ]] && grep -qiE "$MARKER_RE" "$OUT/fixes.md"; then
    echo "[watch] fixes.md bound marker seen at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    head -40 "$OUT/fixes.md" || true
    break
  fi
  # also accept probe mega26_bound from a source land without fixes.md yet
  if git log -1 --oneline 2>/dev/null | grep -qiE 'mega-27|C27|D27|A26|B26'; then
    if [[ -f services/agent/test_mega27_fixes.py ]] || grep -qE 'C27|D27|outline_margin|outline_area|routing_room|retry_ladder|clearance_retry' services/agent/agent_turn.py 2>/dev/null; then
      if [[ ! -f "$OUT/fixes.md" ]]; then
        echo "[watch] source landed but fixes.md missing — still waiting for Fixer27 fixes.md"
      fi
    fi
  fi
  sleep 5
  if (( i % 12 == 0 )); then
    echo "[watch] still waiting… $(date -u +%H:%M:%SZ) iter=$i"
  fi
done

if [[ ! -f "$OUT/fixes.md" ]] || ! grep -qiE "$MARKER_RE" "$OUT/fixes.md"; then
  echo "[watch] TIMEOUT waiting for fixes bound"
  exit 20
fi

exec bash "$OUT/launch_primary.sh"
