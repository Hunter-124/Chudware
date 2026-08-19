#!/usr/bin/env bash
# chudware-web host setup — idempotent. NO secrets written here.
# Usage: bash Webservice/deploy/setup.sh [prod|staging]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="${REPO_ROOT}/Webservice/gateway"
DEPLOY_DIR="${REPO_ROOT}/Webservice/deploy"
DATA_ROOT="${CHUDWARE_WEB_DATA:-${HOME}/chudware-web-data}"
UNIT_DIR="${HOME}/.config/systemd/user"
VENV="${GATEWAY_DIR}/.venv"
ENVIRONMENT="${1:-prod}"
case "$ENVIRONMENT" in
  prod)
    GATEWAY_UNIT="chudware-gateway.service"
    RETENTION_UNIT="chudware-security-retention.service"
    RETENTION_TIMER="chudware-security-retention.timer"
    ENTITLEMENT_CREDENTIAL="${HOME}/.config/credstore.encrypted/chudware-entitlement.cred"
    ;;
  staging)
    GATEWAY_UNIT="chudware-gateway-staging.service"
    RETENTION_UNIT="chudware-security-retention-staging.service"
    RETENTION_TIMER="chudware-security-retention-staging.timer"
    DATA_ROOT="${CHUDWARE_WEB_DATA:-${HOME}/chudware-web-staging}"
    ENTITLEMENT_CREDENTIAL="${HOME}/.config/credstore.encrypted/chudware-entitlement-staging.cred"
    ;;
  *) echo "ERROR: environment must be prod or staging" >&2; exit 2 ;;
esac

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
# chudware-web gateway env — NEVER commit this file.
# Filled by Webservice/deploy/cf_provision.py (driver) and/or hand edits.

# Optional Cloudflare Access fallback, for a caller with no gateway session.
# Leave both blank: /admin is gated by the chudware_sid session plus
# accounts.role='admin', and nothing provisions an Access application.
# CHUDWARE_WEB_CF_TEAM_DOMAIN=https://bruh234324.cloudflareaccess.com
# CHUDWARE_WEB_CF_AUD=

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
  echo "    wrote skeleton ${ENV_FILE} (chmod 600) — fill CHUDWARE_WEB_SESSION_SECRET"
else
  echo "    keep existing ${ENV_FILE}"
fi

# cloudflared gets its own least-privilege environment so the tunnel credential
# is never inherited by uvicorn.
TUNNEL_ENV_FILE="${DATA_ROOT}/cloudflared.env"
if [[ ! -f "${TUNNEL_ENV_FILE}" ]]; then
  printf '# cloudflared only — NEVER commit this file.\nTUNNEL_TOKEN=\n' >"${TUNNEL_ENV_FILE}"
  chmod 600 "${TUNNEL_ENV_FILE}"
  echo "    wrote skeleton ${TUNNEL_ENV_FILE} (chmod 600)"
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
# --- entitlement signing credential ----------------------------------------
# The helper consumes 32 raw bytes on stdin and stores only a TPM2/host-bound
# encrypted systemd credential. It is intentionally interactive and never
# copies signing material into gateway.env, the database, or this repository.
# Environment-specific encrypted credential must exist before service enablement.
if [[ ! -f "${ENTITLEMENT_CREDENTIAL}" || ! -r "${ENTITLEMENT_CREDENTIAL}" ]]; then
  echo "ERROR: encrypted entitlement credential is missing: ${ENTITLEMENT_CREDENTIAL}" >&2
  exit 1
fi
chmod 600 "${ENTITLEMENT_CREDENTIAL}"
echo "    entitlement credential present: ${ENTITLEMENT_CREDENTIAL}"

# --- systemd user units ------------------------------------------------------
mkdir -p "${UNIT_DIR}"
for u in "${GATEWAY_UNIT}" chudware-cloudflared.service chudware-monitor.service chudware-monitor.timer chudware-backup.service chudware-backup.timer "${RETENTION_UNIT}" "${RETENTION_TIMER}"; do
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
systemctl --user disable --now chudware-security-retention.timer chudware-security-retention-staging.timer >/dev/null 2>&1 || true
systemctl --user enable "${GATEWAY_UNIT}" chudware-cloudflared.service chudware-monitor.timer chudware-backup.timer "${RETENTION_UNIT}" "${RETENTION_TIMER}"
echo "    enabled: ${GATEWAY_UNIT}, chudware-cloudflared, chudware-monitor.timer, chudware-backup.timer, ${RETENTION_UNIT}, ${RETENTION_TIMER}"

echo
echo "==> next steps checklist"
cat <<EOF
  [ ] SECURITY: rotate Cloudflare API token + R2 keys (leaked in git history) — see RUNBOOK.md §SECURITY
  [ ] Confirm cf_provision.py wrote TUNNEL_TOKEN into ${TUNNEL_ENV_FILE}
  [ ] Set CHUDWARE_WEB_SESSION_SECRET in ${ENV_FILE} — REQUIRED; the gateway refuses to
      start without it. Generate with:
      python3 -c 'import secrets;print(secrets.token_urlsafe(48))'
  [ ] Optional: Google/GitHub sign-in — set CHUDWARE_WEB_GOOGLE_CLIENT_ID/_SECRET and/or
      CHUDWARE_WEB_GITHUB_CLIENT_ID/_SECRET in ${ENV_FILE} (RUNBOOK.md §(c))
  [ ] Provision the online entitlement credential (exactly 32 raw key bytes on stdin):
      cat /secure/owner-source/entitlement.key | Webservice/deploy/provision_entitlement_credential.sh prod
      TPM2 is preferred; otherwise systemd-creds uses the host key and owner unlock at boot.
  [ ] Nothing to seed: users register themselves and verify by email; approve them at /admin
  [ ] Grant yourself the /admin role — this is the whole operator allowlist now:
      cd Webservice/gateway && .venv/bin/python -m authn grant-admin --email <addr>
      then sign in at /signin and open /admin
  [ ] Delete the leftover app.chudware.com/admin* Cloudflare Access application in
      Zero Trust — while it exists CF shows its login page instead of /signin
  [ ] systemctl --user start ${GATEWAY_UNIT} chudware-cloudflared chudware-monitor.timer ${RETENTION_TIMER}
  [ ] curl -sS http://127.0.0.1:8300/healthz | jq .
  [ ] Full ops: Webservice/deploy/RUNBOOK.md
EOF

echo
echo "setup done."
