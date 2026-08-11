#!/usr/bin/env bash
# chudware-web nightly backup.
#
#   pg_dump -Fc chudware_prod      ─┐
#   pg_dump -Fc chudware_staging   ─┤─► age -r <recipient> ─► ~/chudware-web-backups
#   tar of the non-database state  ─┘                      └─► R2 s3://chudware-backups
#
# Everything is encrypted BEFORE it touches the disk it is backing up and before
# it leaves the box: the pipeline is `producer | age | file`, never a plaintext
# temporary. Installed as a systemd user timer (chudware-backup.timer).
#
# WHAT IS DELIBERATELY NOT IN HERE: secrets/ and the *.env files. They hold the
# PostgreSQL role passwords, the session secret, and the Stripe/OAuth keys —
# i.e. the credentials that protect the very dump they would be sitting next to.
# Shipping both to the same bucket makes one leaked archive both the lock and
# the key. They stay on-box; back them up through the operator's own password
# store, not through this script. Do not "fix" this by removing the excludes.
#
# Restore:
#   age -d -i <identity> < chudware_prod-<stamp>.dump.age > /tmp/prod.dump
#   pg_restore -d chudware_prod --clean --if-exists /tmp/prod.dump
#   age -d -i <identity> < chudware-web-data-<stamp>.tar.age | tar -x -C ~/chudware-web-data
set -euo pipefail

DATA_ROOT="${CHUDWARE_WEB_DATA_ROOT:-$HOME/chudware-web-data}"
DEST="${CHUDWARE_WEB_BACKUP_DIR:-$HOME/chudware-web-backups}"
KEEP="${CHUDWARE_WEB_BACKUP_KEEP:-7}"
SECRETS="$DATA_ROOT/secrets"
BUCKET="${CHUDWARE_BACKUP_BUCKET:-chudware-backups}"
LIFECYCLE_DAYS="${CHUDWARE_BACKUP_LIFECYCLE_DAYS:-30}"
DATABASES=(chudware_prod chudware_staging)
VENV_PY="${CHUDWARE_GATEWAY_PYTHON:-$HOME/Repositories/CAD/Webservice/gateway/.venv/bin/python}"

die() { echo "backup: $*" >&2; exit 1; }

[ -d "$DATA_ROOT" ] || die "no data root at $DATA_ROOT"
command -v pg_dump >/dev/null 2>&1 || die "pg_dump not found (dnf install postgresql16)"
command -v age >/dev/null 2>&1 || die "age not found (dnf install age)"

# Fail closed. An unencrypted backup is worse than no backup: it looks like a
# working control while being a full credential and customer-data leak.
RECIPIENT="${CHUDWARE_BACKUP_AGE_RECIPIENT:-}"
if [ -z "$RECIPIENT" ] && [ -r "$SECRETS/backup_age_recipient" ]; then
  RECIPIENT="$(tr -d '[:space:]' < "$SECRETS/backup_age_recipient")"
fi
[ -n "$RECIPIENT" ] || die "no age recipient: set CHUDWARE_BACKUP_AGE_RECIPIENT or write $SECRETS/backup_age_recipient"

mkdir -p "$DEST"
chmod 700 "$DEST"
# A failed run leaves a *.partial behind, and retention below only matches
# finished artifacts — so without this they accumulate forever and, worse, a
# directory full of them looks like a directory full of backups. The previous
# version of this script had exactly that bug: twelve identically-sized
# .tar.gz.partial files from nights the tar died on an unreadable homes/ file.
rm -f "$DEST"/*.partial
stamp="$(date +%Y%m%d-%H%M%S)"
artifacts=()

# --- databases -------------------------------------------------------------
for dbname in "${DATABASES[@]}"; do
  pwfile="$SECRETS/pg_${dbname#chudware_}_password"
  [ -r "$pwfile" ] || die "missing password file $pwfile"
  out="$DEST/$dbname-$stamp.dump.age"
  # -Fc is the custom format: compressed, and pg_restore can select out of it.
  PGPASSWORD="$(cat "$pwfile")" pg_dump -Fc --no-password \
      -h 127.0.0.1 -U "$dbname" -d "$dbname" \
    | age -r "$RECIPIENT" > "$out.partial"
  mv "$out.partial" "$out"
  chmod 600 "$out"
  artifacts+=("$out")
done

# --- non-database state ----------------------------------------------------
# tmp/ and logs/ are recreatable; secrets/ and *.env are excluded on purpose
# (see the header). Everything else — plm/ bare repos, homes/, loose json — is
# state we cannot rebuild.
out="$DEST/chudware-web-data-$stamp.tar.age"
tar_excludes=(
  --exclude=./tmp
  --exclude=./logs
  --exclude=./secrets
  --exclude='./*.env'
  --exclude='./*.env.bak-*'
)

# homes/ holds each web user's CAD profile, written by hostd under a subuid, so
# some files are simply not readable by the account this timer runs as. tar
# would emit "Permission denied" and exit 2. Elevate rather than skip: a backup
# that silently omits every user's config.json is a backup that fails on the
# day it matters. --ignore-failed-read here would be the wrong fix.
tar_cmd=(tar)
if find "$DATA_ROOT" -path "$DATA_ROOT/tmp" -prune -o \
        -path "$DATA_ROOT/logs" -prune -o \
        -path "$DATA_ROOT/secrets" -prune -o \
        ! -readable -print -quit 2>/dev/null | grep -q .; then
  sudo -n true 2>/dev/null \
    || die "$DATA_ROOT contains files this account cannot read and passwordless sudo is unavailable; the archive would be incomplete"
  tar_cmd=(sudo -n tar)
fi

"${tar_cmd[@]}" -c "${tar_excludes[@]}" -C "$DATA_ROOT" . \
  | age -r "$RECIPIENT" > "$out.partial"
mv "$out.partial" "$out"
chmod 600 "$out"
artifacts+=("$out")

# --- off-box copy ----------------------------------------------------------
# R2 is S3-compatible; boto3 is already a gateway dependency. Skipped (loudly,
# but without failing the local backup) when the keys are not configured.
if [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ] && [ -n "${R2_ENDPOINT:-}" ]; then
  [ -x "$VENV_PY" ] || die "gateway venv python not found at $VENV_PY"
  "$VENV_PY" - "$BUCKET" "$LIFECYCLE_DAYS" "${artifacts[@]}" <<'PY'
import os
import sys

import boto3
from botocore.exceptions import ClientError

bucket, days, *paths = sys.argv[1:]
days = int(days)
# R2 is S3-compatible but not S3: an explicit endpoint, and "auto" as the
# region because R2 has no regions and botocore insists on signing with one.
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="auto",
)

try:
    s3.head_bucket(Bucket=bucket)
except ClientError:
    s3.create_bucket(Bucket=bucket)

# Idempotent: R2 keeps one lifecycle configuration per bucket, so re-PUTting
# the same rule every night is a no-op rather than an accumulation.
s3.put_bucket_lifecycle_configuration(
    Bucket=bucket,
    LifecycleConfiguration={
        "Rules": [
            {
                "ID": "expire-backups",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Expiration": {"Days": days},
            }
        ]
    },
)

for path in paths:
    key = path.rsplit("/", 1)[-1]
    s3.upload_file(path, bucket, key)
    print(f"  uploaded s3://{bucket}/{key}")
PY
else
  echo "backup: R2 keys not configured (R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT) — LOCAL COPY ONLY"
fi

# --- retention -------------------------------------------------------------
# Per family, so a database dump never evicts the data tar or vice versa.
for prefix in "${DATABASES[@]}" chudware-web-data; do
  ls -1t "$DEST/$prefix"-*.age 2>/dev/null | tail -n +$((KEEP + 1)) |
    while read -r old; do rm -f "$old"; done
done

echo "backup ok: ${#artifacts[@]} artifact(s), $(du -ch "${artifacts[@]}" | tail -1 | cut -f1) total, keeping $KEEP locally"
