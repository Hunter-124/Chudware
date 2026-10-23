#!/usr/bin/env bash
# Host hardening for the Chudware production box (Fedora, self-hosted on a LAN).
#
# Dry-run by default. Apply one item, all items, or the commercial-launch profile:
#
#   ./harden_host.sh
#   sudo ./harden_host.sh --apply firewall
#   sudo ./harden_host.sh --apply launch
#
# "launch" includes removing general sudo from the service identity. Operators
# must provision and verify a separate administrator before applying it.
set -euo pipefail

APPLY=0
WHAT="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; WHAT="${2:-all}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SVC_USER="${CHUDWARE_SERVICE_USER:-liveuser}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
SVC_UID="$(id -u "$SVC_USER")"
SAFE_ITEMS=" sudo firewall tunnel updates keys backups gateway entitlement "

note() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
would() { printf '   \033[33mwould:\033[0m %s\n' "$*"; }
did()   { printf '   \033[32mdid:\033[0m   %s\n' "$*"; }
skip()  { printf '   \033[90mskip:\033[0m  %s\n' "$*"; }
die()   { printf '   \033[1;31mrefuse:\033[0m %s\n' "$*" >&2; exit 1; }

selected() {
  [[ "$WHAT" == "all" || "$WHAT" == "$1" ||
     ( "$WHAT" == "launch" && "$SAFE_ITEMS" == *" $1 "* ) ]]
}
want() { selected "$1" && (( APPLY )); }
show() { selected "$1"; }

run() { # run <item> <description> <command...>
  local item="$1" desc="$2"; shift 2
  if want "$item"; then "$@" && did "$desc"; else would "$desc"; fi
}

user_systemctl() {
  runuser -u "$SVC_USER" -- env "XDG_RUNTIME_DIR=/run/user/$SVC_UID" \
    systemctl --user "$@"
}

set_env_value() {
  local path="$1" key="$2" value="$3" tmp
  tmp="$(mktemp)"
  if [[ -f "$path" ]]; then
    awk -v key="$key" -v value="$value" '
      $0 ~ ("^" key "=") {
        if (!written) print key "=" value
        written=1
        next
      }
      { print }
      END { if (!written) print key "=" value }
    ' "$path" > "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp"
  fi
  install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$tmp" "$path"
  rm -f "$tmp"
}

if (( APPLY )) && (( EUID != 0 )); then
  die "--apply must run as root (use sudo)"
fi

# ---------------------------------------------------------------------------
# 1. sudo — least privilege is part of the launch default. Preflight output
# explicitly warns that a separate administrator must exist before applying.
# ---------------------------------------------------------------------------
if show sudo; then
  note "sudo: remove passwordless root for the service account"
  if id -nG "$SVC_USER" 2>/dev/null | tr ' ' '\n' | grep -qx wheel; then
    echo "   $SVC_USER is currently in wheel with NOPASSWD:ALL."
    echo "   REQUIRED: verify another administrator before applying the launch profile."
    run sudo "remove $SVC_USER from wheel" gpasswd -d "$SVC_USER" wheel
    run sudo "require a password for wheel sudo" \
      bash -c 'printf "%%wheel ALL=(ALL:ALL) ALL\n" > /etc/sudoers.d/wheel-nopasswd && visudo -cf /etc/sudoers.d/wheel-nopasswd'
  else
    skip "$SVC_USER is not in wheel"
  fi
fi

# ---------------------------------------------------------------------------
# 2. firewall — Chudware ingress is Cloudflare Tunnel. Preserve SSH before
# touching the default-zone rules, preserve KDE Connect 1716/tcp+udp, and
# default-deny every other zone port or rich-rule exception. Rootful Podman
# publishes for the separately owned mail stack are outside this zone policy;
# they are documented in RUNBOOK.md and must not be disrupted here.
# ---------------------------------------------------------------------------
if show firewall; then
  note "firewall: reduce the default zone to SSH and KDE Connect"
  zone="$(firewall-cmd --get-default-zone 2>/dev/null || true)"
  [[ -n "$zone" ]] || die "cannot determine firewalld's default zone"
  echo "   Default zone: $zone"
  echo "   Current ports: $(firewall-cmd --zone="$zone" --list-ports 2>/dev/null || echo '?')"

  if want firewall; then
    if ! firewall-cmd --zone="$zone" --query-service=ssh >/dev/null &&
       ! firewall-cmd --zone="$zone" --query-port=22/tcp >/dev/null; then
      die "port 22 is not allowed in the active zone; refusing to change or reload firewalld"
    fi
    if ! ss -H -ltn | awk '{print $4}' | grep -Eq '(^|:|\])22$'; then
      die "sshd is not listening on TCP 22; refusing to change or reload firewalld"
    fi
    did "preflight confirmed the active zone allows SSH and sshd listens on TCP 22"

    firewall-cmd --permanent --zone="$zone" --add-service=ssh >/dev/null

    read -r -a ports <<< "$(firewall-cmd --permanent --zone="$zone" --list-ports)"
    for port in "${ports[@]}"; do
      [[ "$port" == "1716/tcp" || "$port" == "1716/udp" ]] && continue
      firewall-cmd --permanent --zone="$zone" --remove-port="$port" >/dev/null
    done
    while IFS= read -r rule; do
      [[ -n "$rule" ]] || continue
      firewall-cmd --permanent --zone="$zone" --remove-rich-rule="$rule" >/dev/null
    done < <(firewall-cmd --permanent --zone="$zone" --list-rich-rules)
    firewall-cmd --permanent --zone="$zone" --add-port=1716/tcp >/dev/null
    firewall-cmd --permanent --zone="$zone" --add-port=1716/udp >/dev/null

    firewall-cmd --reload >/dev/null
    firewall-cmd --zone="$zone" --query-service=ssh >/dev/null ||
      firewall-cmd --zone="$zone" --query-port=22/tcp >/dev/null ||
      die "firewall reload lost the SSH allow rule"
    if ! firewall-cmd --zone="$zone" --query-port=1716/tcp >/dev/null ||
       ! firewall-cmd --zone="$zone" --query-port=1716/udp >/dev/null; then
      die "firewall reload lost the KDE Connect 1716 allow rules"
    fi
    did "reloaded firewalld and immediately confirmed TCP 22 remains allowed"
  else
    would "preserve SSH; allow 1716/tcp+udp; remove all other explicit ports and rich rules; reload"
  fi
fi

# ---------------------------------------------------------------------------
# 3. sshd — deliberately declined for launch. Password auth remains enabled.
# ---------------------------------------------------------------------------
if show sshd; then
  note "sshd: keys only, restrict who may log in"
  echo "   NOTE: confirm a working key login before applying."
  if want sshd; then
    cat > /etc/ssh/sshd_config.d/60-chudware-hardening.conf <<'EOF'
# Added by harden_host.sh.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
AllowTcpForwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
    sshd -t && systemctl reload sshd
    did "wrote 60-chudware-hardening.conf and reloaded sshd"
  else
    would "write 60-chudware-hardening.conf and reload sshd"
  fi
fi

# ---------------------------------------------------------------------------
# 4. tunnel token — EnvironmentFile protects the token at rest and cloudflared
# reads TUNNEL_TOKEN itself, so the secret never appears in process argv.
# ---------------------------------------------------------------------------
if show tunnel; then
  note "cloudflared: keep the tunnel token out of process argv"
  gateway_env="$SVC_HOME/chudware-web-data/gateway.env"
  tunnel_env="$SVC_HOME/chudware-web-data/cloudflared.env"
  unit="$SVC_HOME/.config/systemd/user/chudware-cloudflared.service"
  unit_source="$SCRIPT_DIR/chudware-cloudflared.service"

  if want tunnel; then
    [[ -f "$unit_source" ]] || die "canonical unit missing at $unit_source"
    token_line="$(sed -n '/^TUNNEL_TOKEN=/{p;q;}' "$gateway_env" 2>/dev/null || true)"
    if [[ -z "$token_line" ]]; then
      token_line="$(sed -n '/^TUNNEL_TOKEN=/{p;q;}' "$tunnel_env" 2>/dev/null || true)"
    fi
    [[ "$token_line" == TUNNEL_TOKEN=?* ]] ||
      die "no non-empty TUNNEL_TOKEN found in $gateway_env or $tunnel_env"

    tmp="$(mktemp "${tunnel_env}.tmp.XXXXXX")"
    printf '%s\n' "$token_line" > "$tmp"
    chown "$SVC_USER:$SVC_USER" "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$tunnel_env"

    if [[ -f "$gateway_env" ]]; then
      tmp="$(mktemp "${gateway_env}.tmp.XXXXXX")"
      awk '$0 !~ /^TUNNEL_TOKEN=/' "$gateway_env" > "$tmp"
      chown "$SVC_USER:$SVC_USER" "$tmp"
      chmod 600 "$tmp"
      mv -f "$tmp" "$gateway_env"
    fi

    install -o "$SVC_USER" -g "$SVC_USER" -m 644 "$unit_source" "$unit"
    user_systemctl daemon-reload
    user_systemctl restart chudware-cloudflared.service
    user_systemctl is-active --quiet chudware-cloudflared.service ||
      die "cloudflared did not become active after token migration"
    did "moved TUNNEL_TOKEN to cloudflared.env (0600), removed argv expansion, and reconnected"
  else
    would "move TUNNEL_TOKEN to cloudflared.env (0600), install the hardened unit, and restart"
  fi
fi

# ---------------------------------------------------------------------------
# 5. updates — unattended security updates only.
# ---------------------------------------------------------------------------
if show updates; then
  note "updates: enable unattended security patching"
  if ! command -v dnf-automatic >/dev/null 2>&1; then
    run updates "install dnf-automatic" dnf -y install dnf-automatic
  else
    skip "dnf-automatic already installed"
  fi
  if want updates; then
    cat > /etc/dnf/automatic.conf <<'EOF'
# Chudware production host policy. Unspecified values inherit dnf5 defaults.
[commands]
upgrade_type = security
apply_updates = yes
EOF
    systemctl enable --now dnf-automatic.timer >/dev/null
    systemctl is-enabled --quiet dnf-automatic.timer ||
      die "dnf-automatic.timer did not enable"
    did "configured security-only updates and enabled dnf-automatic.timer"
  else
    would "set upgrade_type=security and apply_updates=yes; enable dnf-automatic.timer"
  fi
fi

# ---------------------------------------------------------------------------
# 6. outbound SSH pivot — remove only the owner-approved key and matching host
# stanza after proving the effective ssh config is hunter@192.168.0.123.
# ---------------------------------------------------------------------------
if show keys; then
  note "service account SSH key: remove the hunter-workstation pivot"
  ssh_dir="$SVC_HOME/.ssh"
  config="$ssh_dir/config"
  key="$ssh_dir/id_ed25519_to_other"
  if [[ -f "$key" ]]; then
    effective="$(runuser -u "$SVC_USER" -- ssh -F "$config" -G other-pc 2>/dev/null)"
    host="$(awk '$1 == "hostname" {print $2; exit}' <<< "$effective")"
    remote_user="$(awk '$1 == "user" {print $2; exit}' <<< "$effective")"
    identity="$(awk '$1 == "identityfile" {print $2; exit}' <<< "$effective")"
    echo "   other-pc resolves to $remote_user@$host with $identity"
    [[ "$host" == "192.168.0.123" && "$remote_user" == "hunter" ]] ||
      die "effective SSH config does not exactly match the owner-approved pivot; deleting nothing"
    case "$identity" in
      "$key"|\~/.ssh/id_ed25519_to_other) ;;
      *) die "effective SSH config does not exactly match the owner-approved pivot; deleting nothing" ;;
    esac

    if want keys; then
      rm -f -- "$key" "$key.pub"
      tmp="$(mktemp)"
      awk '
        /^[[:space:]]*Host[[:space:]]+/ {
          drop=0
          for (i=2; i<=NF; i++) if ($i == "other-pc") drop=1
        }
        !drop { print }
      ' "$config" > "$tmp"
      install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$tmp" "$config"
      rm -f "$tmp"
      [[ ! -e "$key" && ! -e "$key.pub" ]] || die "pivot key removal did not complete"
      did "removed only id_ed25519_to_other, its public key, and the other-pc host stanza"
    else
      would "remove only id_ed25519_to_other, its public key, and the other-pc host stanza"
    fi
  else
    skip "owner-approved outbound pivot key is already absent"
  fi
fi

# ---------------------------------------------------------------------------
# 7. backups — confirm the encrypted backup control; do not create a second
# backup mechanism. R2 remains environment-gated until the account enables it.
# ---------------------------------------------------------------------------
if show backups; then
  note "backups: confirm age encryption and off-box push path"
  backup_dir="$SVC_HOME/chudware-web-backups"
  age_count="$(find "$backup_dir" -maxdepth 1 -type f -name '*.age' 2>/dev/null | wc -l)"
  echo "   encrypted .age artifacts: $age_count"
  user_systemctl is-enabled --quiet chudware-backup.timer &&
    echo "   chudware-backup.timer: enabled" ||
    echo "   chudware-backup.timer: NOT enabled"
  if [[ -x "$SCRIPT_DIR/backup.sh" ]] &&
     grep -q 's3.upload_file' "$SCRIPT_DIR/backup.sh"; then
    echo "   off-box R2 push: implemented (runs when R2 credentials are enabled)"
  else
    echo "   off-box R2 push: NOT found"
  fi
  (( age_count > 0 )) || die "no age-encrypted backup artifacts found"
  did "confirmed encrypted backups; no duplicate backup path added"
fi

# ---------------------------------------------------------------------------
# 8. entitlement credential — the online signing key is available only through
# systemd's private credential directory. Provisioning is owner-interactive;
# hardening verifies custody and refuses legacy raw-secret environment wiring.
# ---------------------------------------------------------------------------
if show entitlement; then
  note "entitlement: verify encrypted systemd credential custody"
  cred="$SVC_HOME/.config/credstore.encrypted/chudware-entitlement.cred"
  env_file="$SVC_HOME/chudware-web-data/gateway.env"
  unit_source="$SCRIPT_DIR/chudware-gateway.service"
  [[ -f "$cred" ]] || die "missing $cred; run provision_entitlement_credential.sh prod as $SVC_USER"
  [[ "$(stat -c '%a' "$cred")" = 600 ]] || die "$cred must have mode 600"
  if grep -q '^CHUDWARE_ENTITLEMENT_KEY_PATH=' "$env_file" 2>/dev/null; then
    die "remove CHUDWARE_ENTITLEMENT_KEY_PATH from gateway.env; only the systemd unit may supply it"
  fi
  grep -q '^LoadCredentialEncrypted=entitlement.key:' "$unit_source" \
    || die "canonical gateway unit does not load the encrypted entitlement credential"
  grep -q '^Environment=CHUDWARE_ENTITLEMENT_KEY_PATH=%d/entitlement.key$' "$unit_source" \
    || die "canonical gateway unit does not point at the private credential directory"
  if want entitlement; then
    install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$cred" "$cred"
    did "verified machine-bound encrypted entitlement credential and systemd-only path"
  else
    would "verify credential mode, reject env-file wiring, and require LoadCredentialEncrypted"
  fi
fi

# ---------------------------------------------------------------------------
# 8. gateway attack surfaces — disable the browser shell and host provider-key
# sharing. The latter prevents one customer from spending the host account.
# ---------------------------------------------------------------------------
if show gateway; then
  note "gateway: disable terminal and shared host provider keys"
  prod_env="$SVC_HOME/chudware-web-data/gateway.env"
  if want gateway; then
    set_env_value "$prod_env" CHUDWARE_WEB_TERMINAL_ENABLED 0
    set_env_value "$prod_env" CHUDWARE_WEB_SHARE_PROVIDER_KEYS 0
    user_systemctl restart chudware-gateway.service
    user_systemctl is-active --quiet chudware-gateway.service ||
      die "production gateway did not become active after hardening flags"
    did "set production flags to 0 and restarted the production gateway; staging has a separate service identity"
  else
    would "set CHUDWARE_WEB_TERMINAL_ENABLED=0 and CHUDWARE_WEB_SHARE_PROVIDER_KEYS=0; restart gateways"
  fi
fi

note "done"
(( APPLY )) || echo "Dry run — nothing changed. Re-run with --apply <item|launch|all>."
