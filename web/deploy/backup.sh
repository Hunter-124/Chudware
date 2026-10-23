#!/usr/bin/env bash
# One configured environment per identity: producers stream directly into age.
# Recovery uses backup_state.py restore-state/restore-database, never a plaintext
# dump or tar extraction into a live root. See RUNBOOK.md for isolated targets.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENVIRONMENT="${CHUDWARE_DEPLOY_ENV:-prod}"
DEST="${CHUDWARE_WEB_BACKUP_DIR:-$HOME/chudware-web-backups}"
LIFECYCLE_DAYS="${CHUDWARE_BACKUP_LIFECYCLE_DAYS:-30}"
VENV_PY="${CHUDWARE_GATEWAY_PYTHON:-$HOME/Repositories/CAD/Webservice/gateway/.venv/bin/python}"
case "$ENVIRONMENT" in
  prod) BUCKET="${CHUDWARE_BACKUP_BUCKET:-chudware-backups}" ;;
  staging)
    BUCKET="${CHUDWARE_BACKUP_BUCKET:-chudware-backups-staging}"
    [[ "$BUCKET" != chudware-backups ]] || { echo "backup: staging cannot use the production backup bucket" >&2; exit 1; }
    ;;
  *) echo "backup: CHUDWARE_DEPLOY_ENV must be prod or staging" >&2; exit 1 ;;
esac

[[ -x "$VENV_PY" ]] || { echo "backup: gateway Python unavailable at $VENV_PY" >&2; exit 1; }
# Capture only committed encrypted paths. No broad stale-partial deletion and no
# sudo fallback: unreadable roots or entries fail closed under this identity.
paths="$("$VENV_PY" "$SCRIPT_DIR/backup_state.py" nightly --environment "$ENVIRONMENT" --destination "$DEST")"
mapfile -t artifacts <<< "$paths"

# --- off-box copy ----------------------------------------------------------
# R2 keys must be environment-scoped by the operator. Lifecycle and object keys
# are also environment-scoped; this run cannot expire another environment.
if [[ -n "${R2_ACCESS_KEY_ID:-}" && -n "${R2_SECRET_ACCESS_KEY:-}" && -n "${R2_ENDPOINT:-}" ]]; then
  "$VENV_PY" - "$BUCKET" "$LIFECYCLE_DAYS" "$ENVIRONMENT" "${artifacts[@]}" <<'PY'
import os
import sys

import boto3
from botocore.exceptions import ClientError

bucket, days, environment, *paths = sys.argv[1:]
days = int(days)
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
s3.put_bucket_lifecycle_configuration(
    Bucket=bucket,
    LifecycleConfiguration={"Rules": [{
        "ID": "expire-" + environment + "-backups",
        "Status": "Enabled",
        "Filter": {"Prefix": environment + "/"},
        "Expiration": {"Days": days},
    }]},
)
for path in paths:
    key = environment + "/" + path.rsplit("/", 1)[-1]
    s3.upload_file(path, bucket, key)
    print(f"  uploaded s3://{bucket}/{key}")
PY
else
  echo "backup: R2 keys not configured (R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT) — LOCAL COPY ONLY"
fi
printf 'backup ok: %s encrypted artifact(s) for %s\n' "${#artifacts[@]}" "$ENVIRONMENT"
