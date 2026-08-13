#!/usr/bin/env bash
# Provision the gateway's online Ed25519 entitlement key as a systemd encrypted
# credential. Run interactively on the gateway host; plaintext is never stored.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: provision_entitlement_credential.sh <prod|staging> [--replace]

Reads exactly 32 raw key bytes from standard input and writes only a
systemd-creds encrypted credential. TPM2 is preferred when available. Without
TPM2, systemd-creds uses the host key and asks the owner for an unlock password;
the resulting machine-bound credential is unlocked at boot/service start.
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
case "$1" in
  prod) output="${HOME}/.config/credstore.encrypted/chudware-entitlement.cred" ;;
  staging) output="${HOME}/.config/credstore.encrypted/chudware-entitlement-staging.cred" ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
shift
replace=0
if [[ "${1:-}" == --replace ]]; then replace=1; shift; fi
[[ $# -eq 0 ]] || { usage >&2; exit 2; }
command -v systemd-creds >/dev/null || { echo "ERROR: systemd-creds is required" >&2; exit 1; }
[[ $replace -eq 1 || ! -e "$output" ]] || {
  echo "ERROR: credential already exists; use --replace for an intentional rotation" >&2
  exit 1
}

umask 077
mkdir -p "$(dirname "$output")"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cat >"$tmp"
[[ "$(wc -c <"$tmp")" -eq 32 ]] || {
  echo "ERROR: entitlement private key must be exactly 32 raw bytes" >&2
  exit 1
}
chmod 600 "$tmp"

args=(encrypt --name=entitlement.key)
if [[ -r /sys/class/tpm/tpm0/device/description || -e /dev/tpmrm0 ]]; then
  args+=(--with-key=tpm2)
  echo "TPM2 detected: sealing entitlement credential to this host."
else
  args+=(--with-key=host+tpm2-absent)
  echo "TPM2 unavailable: using the machine-bound host key."
  echo "Enter an owner-held password if systemd-creds prompts; do not save it in an env file."
fi
systemd-creds "${args[@]}" "$tmp" "${output}.new"
chmod 600 "${output}.new"
mv -f "${output}.new" "$output"
echo "Installed encrypted entitlement credential: $output"
echo "Restart the matching gateway unit and confirm it becomes active."
