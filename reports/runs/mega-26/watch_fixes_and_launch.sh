#!/usr/bin/env bash
# Wait for Fixer26 fixes.md 'fixes bound' then launch primary with liveness.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
OUT=reports/runs/mega-26
PY="${HOME}/.local/share/AI-CAD/mcp-venv/bin/python"
MARKER_RE='fixes bound|A26.*bound|B26.*bound|BOUND'

echo "[watch] waiting for $OUT/fixes.md with bound marker (Fixer26=DeadOpossum)"
for i in $(seq 1 720); do  # up to ~60 min @5s
  if [[ -f "$OUT/fixes.md" ]] && grep -qiE "$MARKER_RE" "$OUT/fixes.md"; then
    echo "[watch] fixes.md bound marker seen at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    head -40 "$OUT/fixes.md" || true
    break
  fi
  # also accept probe mega26_bound from a source land without fixes.md yet
  if git log -1 --oneline 2>/dev/null | grep -qiE 'mega-26|A26|B26'; then
    if [[ -f services/agent/test_mega26_fixes.py ]] || grep -qE 'A26|B26|u1_stub|measured_net|clearance_retry' services/agent/agent_turn.py 2>/dev/null; then
      if [[ ! -f "$OUT/fixes.md" ]]; then
        echo "[watch] source landed but fixes.md missing — still waiting for Fixer26 fixes.md"
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
