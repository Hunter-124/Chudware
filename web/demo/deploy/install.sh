#!/usr/bin/env bash
# Install or update the Chudware public demo front door on this host (idempotent).
#
#   sudo web/demo/deploy/install.sh [--no-pull] [--no-start]
#
# Creates the dedicated `chudware-demo` user (rootless podman and the front door both run
# as it, so the egress firewall can single out its traffic), installs the front door under
# /opt/chudware-demo, seeds /etc/chudware-demo/demo.env (never overwritten), loads the
# egress firewall, pulls the session image and enables the user service.
# Run from a checkout; nothing under the checkout is used at runtime.
set -euo pipefail

DEMO_USER=chudware-demo
DEMO_HOME=/var/lib/chudware-demo
APP_DIR=/opt/chudware-demo
ETC_DIR=/etc/chudware-demo
ENV_FILE=$ETC_DIR/demo.env
PYTHON=${PYTHON:-python3}

die() { echo "install.sh: $*" >&2; exit 1; }
step() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
pull=1
start=1
for arg in "$@"; do
    case $arg in
        --no-pull) pull=0 ;;
        --no-start) start=0 ;;
        *) die "unknown argument: $arg" ;;
    esac
done

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
demo_src=$(dirname -- "$here")   # web/demo
web_src=$(dirname -- "$demo_src") # web
for path in "$demo_src/app.py" "$here/egress.nft" "$here/chudware-demo.service" \
            "$demo_src/config.example.env" "$web_src/gateway/requirements.txt"; do
    [[ -e $path ]] || die "missing $path; run this script from a complete checkout"
done
for tool in useradd usermod loginctl systemctl nft podman newuidmap runuser tar install; do
    command -v "$tool" >/dev/null || die "required tool not found: $tool"
done

# -- user ------------------------------------------------------------------------------
# A regular (non-system) account: rootless podman needs subordinate id ranges and a
# systemd user manager, both most dependable for a normal uid. nologin shell, locked
# password, no way to log in.
if ! getent passwd "$DEMO_USER" >/dev/null; then
    step "creating user $DEMO_USER"
    useradd --create-home --home-dir "$DEMO_HOME" --shell /usr/sbin/nologin \
        --comment "Chudware public demo" "$DEMO_USER"
fi
chmod 0700 "$DEMO_HOME"
uid=$(id -u "$DEMO_USER")

ensure_subid() { # <file> <usermod flag>
    local file=$1 flag=$2 first
    touch "$file"
    if ! grep -q "^${DEMO_USER}:" "$file"; then
        first=$(awk -F: 'BEGIN { top = 100000 } { end = $2 + $3; if (end > top) top = end } END { print top }' "$file")
        step "allocating subordinate ids $first-$((first + 65535)) in $file"
        usermod "$flag" "${first}-$((first + 65535))" "$DEMO_USER"
    fi
}
ensure_subid /etc/subuid --add-subuids
ensure_subid /etc/subgid --add-subgids

step "enabling linger and starting the user manager"
loginctl enable-linger "$DEMO_USER"
systemctl start "user@${uid}.service"

as_demo() { # cd /: the checkout's directory is usually not readable by the demo user
    (cd / && runuser -u "$DEMO_USER" -- env HOME="$DEMO_HOME" XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@")
}
user_systemctl() { systemctl --machine="${DEMO_USER}@.host" --user "$@"; }

# -- egress firewall (before anything that can start a container) -----------------------
step "installing the egress firewall"
install -d -m 0755 "$ETC_DIR"
install -m 0644 "$here/egress.nft" "$ETC_DIR/egress.nft"
sed "s|__UID__|$uid|g" "$here/chudware-demo-egress.service" >/etc/systemd/system/chudware-demo-egress.service
chmod 0644 /etc/systemd/system/chudware-demo-egress.service
systemctl daemon-reload
systemctl enable chudware-demo-egress.service
systemctl restart chudware-demo-egress.service
nft list table inet chudware_demo >/dev/null || die "egress table did not load"

# -- application -------------------------------------------------------------------------
step "installing the front door into $APP_DIR"
install -d -m 0755 "$APP_DIR"
staging=$(mktemp -d "$APP_DIR/.demo.XXXXXX")
trap 'rm -rf "$staging"' EXIT
tar -C "$web_src" --exclude='__pycache__' --exclude='demo/tests' --exclude='demo/deploy' \
    --exclude='demo/.venv' -cf - demo | tar -C "$staging" -xf -
rm -rf "$APP_DIR/demo"
mv "$staging/demo" "$APP_DIR/demo"
chown -R root:root "$APP_DIR/demo"
chmod -R go-w,a+rX "$APP_DIR/demo"
[[ -x $APP_DIR/venv/bin/python ]] || "$PYTHON" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check --quiet \
    -r "$web_src/gateway/requirements.txt"
if command -v restorecon >/dev/null; then restorecon -R "$APP_DIR" || true; fi

# -- configuration (never overwrite) -------------------------------------------------------
if [[ ! -e $ENV_FILE ]]; then
    step "creating $ENV_FILE"
    key=$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')
    (umask 077 && sed "s|^CHUDWARE_DEMO_COOKIE_KEY=.*|CHUDWARE_DEMO_COOKIE_KEY=${key}|" \
        "$demo_src/config.example.env" >"$ENV_FILE")
fi
chown "$DEMO_USER:$DEMO_USER" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

# -- user service ----------------------------------------------------------------------------
step "installing the user service"
unit_dir=$DEMO_HOME/.config/systemd/user
install -d -m 0755 -o "$DEMO_USER" -g "$DEMO_USER" \
    "$DEMO_HOME/.config" "$DEMO_HOME/.config/systemd" "$unit_dir"
sed "s|@APP@|$APP_DIR|g" "$here/chudware-demo.service" >"$unit_dir/chudware-demo.service"
chown "$DEMO_USER:$DEMO_USER" "$unit_dir/chudware-demo.service"
chmod 0644 "$unit_dir/chudware-demo.service"
user_systemctl daemon-reload
user_systemctl enable chudware-demo.service

# -- session image -------------------------------------------------------------------------------
if [[ $pull -eq 1 ]]; then
    tag=$(sed -n 's/^CHUDWARE_DEMO_IMAGE_TAG=//p' "$ENV_FILE" | tail -n 1)
    repo=$(sed -n 's/^CHUDWARE_DEMO_IMAGE_REPO=//p' "$ENV_FILE" | tail -n 1)
    image="${repo:-ghcr.io/hunter-124/chudware-web}:${tag:-edge}"
    step "pulling $image as $DEMO_USER (sessions never pull)"
    as_demo podman pull "$image"
fi

if [[ $start -eq 1 ]]; then
    step "starting the front door"
    user_systemctl restart chudware-demo.service
    sleep 2
    user_systemctl --no-pager --lines=5 status chudware-demo.service || true
fi

cat <<EOF

Done. Remaining manual steps:
  * Put the OpenRouter key in $ENV_FILE (CHUDWARE_DEMO_OPENROUTER_KEY=...), then
      sudo systemctl --machine=${DEMO_USER}@.host --user restart chudware-demo.service
  * Point cloudflared (app.chudware.com) at http://127.0.0.1:8300.
  * Logs: sudo journalctl _UID=$uid -u chudware-demo.service  (or --user-unit via machinectl)
  * See web/demo/README.md for the verification checklist.
EOF
