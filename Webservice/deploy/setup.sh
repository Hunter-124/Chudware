#!/usr/bin/env bash
# billet-web host setup — idempotent. NO secrets written here.
# Usage: bash Webservice/deploy/setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="${REPO_ROOT}/Webservice/gateway"
DEPLOY_DIR="${REPO_ROOT}/Webservice/deploy"
DATA_ROOT="${BILLET_WEB_DATA:-${HOME}/billet-web-data}"
UNIT_DIR="${HOME}/.config/systemd/user"
VENV="${GATEWAY_DIR}/.venv"

echo "==> billet-web setup"
echo "    repo:  ${REPO_ROOT}"
echo "    data:  ${DATA_ROOT}"

# --- data root tree ----------------------------------------------------------
mkdir -p \
  "${DATA_ROOT}/homes" \
  "${DATA_ROOT}/plm" \
  "${DATA_ROOT}/logs" \
  "${DATA_ROOT}/secrets"
chmod 700 "${DATA_ROOT}/secrets" 2>/dev/null || true

# gateway.env skeleton (never overwrite existing — secrets live here)
ENV_FILE="${DATA_ROOT}/gateway.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  cat >"${ENV_FILE}" <<'EOF'
# billet-web gateway + cloudflared env — NEVER commit this file.
# Filled by Webservice/deploy/cf_provision.py (driver) and/or hand edits.

# Cloudflare tunnel (remotely-managed); from cf_provision / dashboard
TUNNEL_TOKEN=

# Access JWT verification (team domain + Application Audience AUD tag)
BILLET_CF_TEAM_DOMAIN=https://bruh234324.cloudflareaccess.com
BILLET_CF_AUD=

# Local-only bypass (never on a public path)
# BILLET_WEB_DEV=1

# Optional overrides
# BILLET_WEB_DATA=/home/YOU/billet-web-data
# BILLET_MAX_SESSIONS=3
EOF
  chmod 600 "${ENV_FILE}"
  echo "    wrote skeleton ${ENV_FILE} (chmod 600) — fill TUNNEL_TOKEN + BILLET_CF_AUD"
else
  echo "    keep existing ${ENV_FILE}"
fi

# --- python venv for gateway -------------------------------------------------
if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "==> creating gateway venv"
  python3 -m venv "${VENV}"
fi
echo "==> installing gateway requirements"
"${VENV}/bin/pip" install --upgrade pip -q
"${VENV}/bin/pip" install -r "${GATEWAY_DIR}/requirements.txt" -q
echo "    venv ok: ${VENV}"

# --- systemd user units ------------------------------------------------------
mkdir -p "${UNIT_DIR}"
for u in billet-gateway.service billet-cloudflared.service billet-monitor.service billet-monitor.timer billet-backup.service billet-backup.timer billet-webd-sync.path billet-webd-sync.service; do
  src="${DEPLOY_DIR}/${u}"
  dst="${UNIT_DIR}/${u}"
  if [[ -f "${src}" ]]; then
    # Install as a copy so unit paths stay absolute under %h/Repositories/CAD
    install -m 644 "${src}" "${dst}"
    echo "    installed ${dst}"
  else
    echo "    WARN missing ${src}" >&2
  fi
done

# linger so user units survive logout
if command -v loginctl >/dev/null 2>&1; then
  if [[ "$(loginctl show-user "${USER}" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
    echo "==> enabling linger for ${USER} (may need sudo once)"
    if loginctl enable-linger "${USER}" 2>/dev/null; then
      echo "    linger=yes"
    else
      echo "    run: sudo loginctl enable-linger ${USER}"
    fi
  else
    echo "    linger already yes"
  fi
fi

systemctl --user daemon-reload
systemctl --user enable billet-gateway.service billet-cloudflared.service billet-monitor.timer billet-backup.timer
# Path unit: re-promote ~/billet-web when core/build-occt-linux rebuilds aicad-hostd.
systemctl --user enable --now billet-webd-sync.path >/dev/null 2>&1 || true
echo "    enabled: billet-gateway, billet-cloudflared, billet-monitor.timer, billet-backup.timer, billet-webd-sync.path"

# --- image reminder (no build here) ------------------------------------------
echo
echo "==> podman image (manual / SessionImage)"
echo "    expected: localhost/billet-session:latest"
if podman image exists localhost/billet-session:latest 2>/dev/null; then
  echo "    present: localhost/billet-session:latest"
else
  echo "    MISSING — build when image is ready, e.g.:"
  echo "      podman build -t localhost/billet-session:latest -f Webservice/image/Containerfile Webservice/image"
  echo "    (or pull if published)"
fi

# GPU CDI quick check
if command -v nvidia-ctk >/dev/null 2>&1; then
  if [[ -f /etc/cdi/nvidia.yaml ]] || [[ -f /var/run/cdi/nvidia.yaml ]]; then
    echo "    CDI nvidia device yaml present"
  else
    echo "    CDI yaml not found — after driver updates run:"
    echo "      sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
  fi
fi

echo
echo "==> next steps checklist"
cat <<EOF
  [ ] SECURITY: rotate Cloudflare API token + R2 keys (leaked in git history) — see RUNBOOK.md §SECURITY
  [ ] Confirm cf_provision.py wrote TUNNEL_TOKEN + BILLET_CF_AUD into ${ENV_FILE}
  [ ] Edit Webservice/gateway/users.yaml (emails + heavy seats)
  [ ] Optional: Google IdP in CF Zero Trust (GCP OAuth client) — RUNBOOK.md
  [ ] Edit Access allowlist emails (dashboard or API)
  [ ] Pre-pull/build session image; optional warm seat
  [ ] systemctl --user start billet-gateway billet-cloudflared billet-monitor.timer
  [ ] curl -sS http://127.0.0.1:8300/healthz | jq .
  [ ] Full ops: Webservice/deploy/RUNBOOK.md
EOF

echo
echo "setup done."
