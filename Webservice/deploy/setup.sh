#!/usr/bin/env bash
# chudware-web host setup — idempotent. NO secrets written here.
# Usage: bash Webservice/deploy/setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="${REPO_ROOT}/Webservice/gateway"
DEPLOY_DIR="${REPO_ROOT}/Webservice/deploy"
DATA_ROOT="${CHUDWARE_WEB_DATA:-${HOME}/chudware-web-data}"
UNIT_DIR="${HOME}/.config/systemd/user"
VENV="${GATEWAY_DIR}/.venv"

echo "==> chudware-web setup"
echo "    repo:  ${REPO_ROOT}"
echo "    data:  ${DATA_ROOT}"

# --- data root tree ----------------------------------------------------------
mkdir -p \
  "${DATA_ROOT}/plm" \
  "${DATA_ROOT}/logs" \
  "${DATA_ROOT}/secrets"
chmod 700 "${DATA_ROOT}/secrets" 2>/dev/null || true

# Shared team workspace (gateway /workspace/*). ME489 default matches
# install_webd.sh; create empty so a fresh/migrated host doesn't 503 list.
WORKSPACE_ROOT="${CHUDWARE_WORKSPACE_ROOT:-${HOME}/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build}"
mkdir -p "${WORKSPACE_ROOT}"
echo "    workspace: ${WORKSPACE_ROOT}"

# gateway.env skeleton (never overwrite existing — secrets live here)
ENV_FILE="${DATA_ROOT}/gateway.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  cat >"${ENV_FILE}" <<'EOF'
# chudware-web gateway + cloudflared env — NEVER commit this file.
# Filled by Webservice/deploy/cf_provision.py (driver) and/or hand edits.

# Cloudflare tunnel (remotely-managed); from cf_provision / dashboard
TUNNEL_TOKEN=

# Access JWT verification (team domain + Application Audience AUD tag)
CHUDWARE_CF_TEAM_DOMAIN=https://bruh234324.cloudflareaccess.com
CHUDWARE_CF_AUD=

# Self-serve auth: signs the chudware_sid session cookie (required in production)
CHUDWARE_WEB_SESSION_SECRET=

# Optional social sign-in (blank => email + password registration only)
# CHUDWARE_WEB_GOOGLE_CLIENT_ID=
# CHUDWARE_WEB_GOOGLE_CLIENT_SECRET=
# CHUDWARE_WEB_GITHUB_CLIENT_ID=
# CHUDWARE_WEB_GITHUB_CLIENT_SECRET=

# Local-only bypass (never on a public path)
# CHUDWARE_WEB_DEV=1

# Optional overrides
# CHUDWARE_WEB_DATA=/home/YOU/chudware-web-data
EOF
  chmod 600 "${ENV_FILE}"
  echo "    wrote skeleton ${ENV_FILE} (chmod 600) — fill TUNNEL_TOKEN, CHUDWARE_CF_AUD, CHUDWARE_WEB_SESSION_SECRET"
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
for u in chudware-gateway.service chudware-cloudflared.service chudware-monitor.service chudware-monitor.timer chudware-backup.service chudware-backup.timer; do
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
systemctl --user enable chudware-gateway.service chudware-cloudflared.service chudware-monitor.timer chudware-backup.timer
echo "    enabled: chudware-gateway, chudware-cloudflared, chudware-monitor.timer, chudware-backup.timer"

echo
echo "==> next steps checklist"
cat <<EOF
  [ ] SECURITY: rotate Cloudflare API token + R2 keys (leaked in git history) — see RUNBOOK.md §SECURITY
  [ ] Confirm cf_provision.py wrote TUNNEL_TOKEN + CHUDWARE_CF_AUD into ${ENV_FILE}
  [ ] Set CHUDWARE_WEB_SESSION_SECRET in ${ENV_FILE} — REQUIRED; the gateway refuses to
      start without it. Generate with:
      python3 -c 'import secrets;print(secrets.token_urlsafe(48))'
  [ ] Optional: Google/GitHub sign-in — set CHUDWARE_WEB_GOOGLE_CLIENT_ID/_SECRET and/or
      CHUDWARE_WEB_GITHUB_CLIENT_ID/_SECRET in ${ENV_FILE} (RUNBOOK.md §(c))
  [ ] Nothing to seed: users register themselves and verify by email; approve them at /admin
  [ ] Optional: Google IdP for the CF Access /admin door (CF Zero Trust) — RUNBOOK.md
  [ ] Edit Access allowlist emails — Access gates /admin only (dashboard or API)
  [ ] systemctl --user start chudware-gateway chudware-cloudflared chudware-monitor.timer
  [ ] curl -sS http://127.0.0.1:8300/healthz | jq .
  [ ] Full ops: Webservice/deploy/RUNBOOK.md
EOF

echo
echo "setup done."
