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
  prod) environment=prod; output="${HOME}/.config/credstore.encrypted/chudware-entitlement.cred" ;;
  staging) environment=staging; output="${HOME}/.config/credstore.encrypted/chudware-entitlement-staging.cred" ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
shift
replace=0
if [[ "${1:-}" == --replace ]]; then replace=1; shift; fi
[[ $# -eq 0 ]] || { usage >&2; exit 2; }
command -v systemd-creds >/dev/null || { echo "ERROR: systemd-creds is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 is required" >&2; exit 1; }
if [[ "$environment" = staging ]]; then
  [[ "$(id -un)" = chudware-staging && "$HOME" = /var/lib/chudware-staging ]] || {
    echo "ERROR: staging provisioning requires the dedicated chudware-staging account and home" >&2
    exit 2
  }
elif [[ "$(id -un)" = chudware-staging ]]; then
  echo "ERROR: staging identity cannot provision production" >&2
  exit 2
fi
[[ $replace -eq 1 || ! -e "$output" ]] || {
  echo "ERROR: credential already exists; use --replace for an intentional rotation" >&2
  exit 1
}

umask 077

args=(encrypt --name=entitlement.key)
if [[ -r /sys/class/tpm/tpm0/device/description || -e /dev/tpmrm0 ]]; then
  args+=(--with-key=tpm2)
  echo "TPM2 detected: sealing entitlement credential to this host."
else
  args+=(--with-key=host+tpm2-absent)
  echo "TPM2 unavailable: using the machine-bound host key."
  echo "Enter an owner-held password if systemd-creds prompts; do not save it in an env file."
fi
# Python holds the bounded raw input in memory; only the encrypted output gets
# a temporary file. Never put binary key material in shell variables or argv.
python3 -c '
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

output = Path(sys.argv[1])
replace = sys.argv[2] == "1"
key = sys.stdin.buffer.read(33)
if len(key) != 32:
    sys.exit("ERROR: entitlement private key must be exactly 32 raw bytes")

uid = os.getuid()
for directory in (output.parents[2], output.parents[1], output.parent):
    if not directory.exists():
        directory.mkdir(mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        sys.exit("ERROR: credential directory must be owned by this user and not writable by others")
if stat.S_IMODE(output.parent.lstat().st_mode) != 0o700:
    sys.exit("ERROR: encrypted credential directory must have mode 700")
if output.exists() or output.is_symlink():
    info = output.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        sys.exit("ERROR: existing credential must be a user-owned regular mode-600 file")
    if not replace:
        sys.exit("ERROR: credential already exists; use --replace for an intentional rotation")

fd, name = tempfile.mkstemp(prefix=output.name + ".", suffix=".new", dir=output.parent)
os.close(fd)
temporary = Path(name)
try:
    subprocess.run(["systemd-creds", *sys.argv[3:], "-", str(temporary)],
                   input=key, check=True)
    temporary.chmod(0o600)
    if replace:
        os.replace(temporary, output)
    else:
        # Atomic no-clobber placement, including a competing first provision.
        os.link(temporary, output, follow_symlinks=False)
finally:
    temporary.unlink(missing_ok=True)
' "$output" "$replace" "${args[@]}"
echo "Installed encrypted entitlement credential: $output"
echo "Restart the matching gateway unit and confirm it becomes active."
