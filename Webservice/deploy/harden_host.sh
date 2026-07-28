#!/usr/bin/env bash
# Host hardening for the chudware-web box (Fedora, self-hosted on a home LAN).
#
# Everything here addresses a finding from the 2026-07-28 security audit. The
# script is DRY-RUN BY DEFAULT and each item is individually opt-in, because
# several of them can lock you out of a machine you are sitting in front of.
#
#   ./harden_host.sh                 # show what would change, touch nothing
#   ./harden_host.sh --apply sudo    # apply one item
#   ./harden_host.sh --apply all     # apply everything (read it first)
#
# Run as root on the host itself, or via: livingroom 'bash -s' < harden_host.sh
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
ADMIN_IP="${CHUDWARE_ADMIN_CIDR:-192.168.0.0/24}"

note() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
would() { printf '   \033[33mwould:\033[0m %s\n' "$*"; }
did()   { printf '   \033[32mdid:\033[0m   %s\n' "$*"; }
skip()  { printf '   \033[90mskip:\033[0m  %s\n' "$*"; }

want() { [[ "$WHAT" == "all" || "$WHAT" == "$1" ]] && (( APPLY )); }
show() { [[ "$WHAT" == "all" || "$WHAT" == "$1" ]]; }

run() { # run <item> <description> <command...>
  local item="$1" desc="$2"; shift 2
  if want "$item"; then "$@" && did "$desc"; else would "$desc"; fi
}

# ---------------------------------------------------------------------------
# 1. sudo — FINDING: `%wheel ALL=(ALL:ALL) NOPASSWD: ALL` and the gateway
#    account is in wheel. Any web-app RCE is therefore instant, silent root.
#    This is the single largest blast-radius multiplier on the host.
# ---------------------------------------------------------------------------
if show sudo; then
  note "sudo: remove passwordless root for the service account"
  if id -nG "$SVC_USER" 2>/dev/null | tr ' ' '\n' | grep -qx wheel; then
    echo "   $SVC_USER is currently in 'wheel' with NOPASSWD:ALL."
    echo "   NOTE: if you log into the desktop as $SVC_USER you will need another"
    echo "         admin account, or sudo will stop working for your desktop session."
    run sudo "gpasswd -d $SVC_USER wheel" gpasswd -d "$SVC_USER" wheel
    run sudo "require a password for wheel sudo" \
        bash -c 'printf "%%wheel ALL=(ALL:ALL) ALL\n" > /etc/sudoers.d/wheel-nopasswd && visudo -cf /etc/sudoers.d/wheel-nopasswd'
  else
    skip "$SVC_USER is not in wheel"
  fi
fi

# ---------------------------------------------------------------------------
# 2. firewall — FINDING: the default FedoraWorkstation zone opens
#    1025-65535/tcp AND /udp to the entire LAN. Every stray dev server,
#    container port and debug listener on this box is exposed to the network.
#    The web service does NOT need this: it is reached only via the tunnel.
# ---------------------------------------------------------------------------
if show firewall; then
  note "firewall: close the wide-open 1025-65535 LAN range"
  echo "   Current: $(firewall-cmd --list-ports --zone=FedoraWorkstation 2>/dev/null || echo '?')"
  echo "   NOTE: this may break KDE Connect (1716) and mDNS-discovered peers."
  echo "         Re-add only what you actually use."
  run firewall "remove 1025-65535/tcp" firewall-cmd --permanent --zone=FedoraWorkstation --remove-port=1025-65535/tcp
  run firewall "remove 1025-65535/udp" firewall-cmd --permanent --zone=FedoraWorkstation --remove-port=1025-65535/udp
  run firewall "re-add KDE Connect 1716 (tcp+udp)" bash -c \
      'firewall-cmd --permanent --zone=FedoraWorkstation --add-port=1716/tcp
       firewall-cmd --permanent --zone=FedoraWorkstation --add-port=1716/udp'
  run firewall "reload firewalld" firewall-cmd --reload
fi

# ---------------------------------------------------------------------------
# 3. sshd — FINDING: PasswordAuthentication is on with no AllowUsers, so every
#    account on the box is brute-forceable from the LAN. Combined with (1)
#    that was a direct path to root.
# ---------------------------------------------------------------------------
if show sshd; then
  note "sshd: keys only, restrict who may log in"
  echo "   NOTE: confirm you have a working key login BEFORE applying, or you"
  echo "         will lock yourself out of remote access."
  if want sshd; then
    cat > /etc/ssh/sshd_config.d/60-chudware-hardening.conf <<EOF
# Added by harden_host.sh — 2026-07-28 security audit.
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
    sshd -t && systemctl reload sshd && did "wrote 60-chudware-hardening.conf and reloaded sshd"
  else
    would "write /etc/ssh/sshd_config.d/60-chudware-hardening.conf (keys only, no X11/TCP fwd, MaxAuthTries 3)"
  fi
fi

# ---------------------------------------------------------------------------
# 4. tunnel token — FINDING: `cloudflared tunnel run --token <JWT>` puts the
#    tunnel token in the process argv, readable by any local user via `ps`.
#    Whoever reads it can stand up their own tunnel for your hostnames.
# ---------------------------------------------------------------------------
if show tunnel; then
  note "cloudflared: keep the tunnel token out of process argv"
  unit="/home/$SVC_USER/.config/systemd/user/chudware-cloudflared.service"
  if grep -q -- '--token \${TUNNEL_TOKEN}' "$unit" 2>/dev/null; then
    echo "   Token is currently visible in 'ps aux' output."
    would "replace ExecStart with a --credentials-file invocation (see DEPLOYMENT.md)"
    echo "   Manual step: cloudflared writes ~/.cloudflared/<tunnel-id>.json when you"
    echo "   run 'cloudflared tunnel login && cloudflared tunnel token --cred-file ...'."
    echo "   Then: ExecStart=/usr/local/bin/cloudflared tunnel run --credentials-file %h/.cloudflared/<id>.json <name>"
    echo "   and chmod 600 that file. Left manual because it needs a CF login."
  else
    skip "token is not in argv"
  fi
fi

# ---------------------------------------------------------------------------
# 5. updates — FINDING: no dnf-automatic; the host had no unattended security
#    patching at all.
# ---------------------------------------------------------------------------
if show updates; then
  note "updates: enable unattended security patching"
  if ! rpm -q dnf-automatic >/dev/null 2>&1; then
    run updates "install dnf-automatic" dnf -y install dnf-automatic
  else
    skip "dnf-automatic already installed"
  fi
  if want updates; then
    sed -i 's/^upgrade_type *=.*/upgrade_type = security/; s/^apply_updates *=.*/apply_updates = yes/' \
        /etc/dnf/automatic.conf 2>/dev/null || true
    systemctl enable --now dnf-automatic.timer && did "enabled dnf-automatic.timer (security only)"
  else
    would "set upgrade_type=security, apply_updates=yes; enable dnf-automatic.timer"
  fi
fi

# ---------------------------------------------------------------------------
# 6. service-account SSH keys — FINDING: the gateway account holds private keys
#    to another LAN machine (192.168.0.123 as user 'hunter'). A shell obtained
#    through the admin terminal is a passwordless pivot into the home network.
#    NOT automated: moving keys can break your own automation. Reported only.
# ---------------------------------------------------------------------------
if show keys; then
  note "service account SSH keys: LAN pivot material (manual review)"
  # id_* and *_to_* overlap, so dedupe before reporting.
  while IFS= read -r k; do
    [[ -f "$k" && "$k" != *.pub ]] || continue
    echo "   private key: $k  ($(stat -c '%a %U:%G' "$k"))"
  done < <(printf '%s\n' /home/"$SVC_USER"/.ssh/id_* /home/"$SVC_USER"/.ssh/*_to_* | sort -u)
  if [[ -f /home/$SVC_USER/.ssh/config ]]; then
    echo "   ~/.ssh/config declares these hosts:"
    grep -iE '^\s*(Host|HostName)\b' /home/"$SVC_USER"/.ssh/config | sed 's/^/     /'
  fi
  echo "   ACTION: move outbound keys to a separate operator account. The account"
  echo "   that runs an internet-facing service should hold no credentials to"
  echo "   anything else on the LAN."
fi

# ---------------------------------------------------------------------------
# 7. backups — FINDING: nightly tarballs contain every secret and are written
#    to the SAME disk as the service. No off-box copy, no encryption.
# ---------------------------------------------------------------------------
if show backups; then
  note "backups: secrets-bearing tarballs are local-only and unencrypted"
  d="/home/$SVC_USER/chudware-web-backups"
  [[ -d "$d" ]] && echo "   $(ls -1 "$d" | wc -l) archives in $d ($(du -sh "$d" 2>/dev/null | cut -f1))"
  echo "   ACTION: pipe backup.sh output through 'age -r <recipient>' or 'gpg -e'"
  echo "   and copy off-box (R2, or rsync to another machine with a dedicated key)."
  echo "   Until then a stolen disk is a full credential compromise."
fi

note "done"
(( APPLY )) || echo "Dry run — nothing changed. Re-run with --apply <item|all>."
