#!/usr/bin/env bash
# chudware-web nightly backup — tars ~/chudware-web-data (PLM bare repos, users.yaml,
# chat history) into ~/chudware-web-backups with retention.
# Installed as a systemd user timer (chudware-backup.timer) by setup.sh.
set -euo pipefail

DATA_ROOT="${CHUDWARE_WEB_DATA_ROOT:-$HOME/chudware-web-data}"
DEST="${CHUDWARE_WEB_BACKUP_DIR:-$HOME/chudware-web-backups}"
KEEP="${CHUDWARE_WEB_BACKUP_KEEP:-14}"

[ -d "$DATA_ROOT" ] || { echo "no data root at $DATA_ROOT; nothing to back up"; exit 0; }
mkdir -p "$DEST"
chmod 700 "$DEST"

stamp="$(date +%Y%m%d-%H%M%S)"
out="$DEST/chudware-web-data-$stamp.tar.gz"
tmp="$out.partial"

# Caches are junk; everything else (plm/, users.yaml, chat.db, *.env) is state
# we cannot recreate. Restore with:  tar -xzf <backup> -C ~
tar -czf "$tmp" \
    --exclude='tmp' \
    --exclude='logs' \
    -C "$(dirname "$DATA_ROOT")" "$(basename "$DATA_ROOT")"
mv "$tmp" "$out"
chmod 600 "$out"

# Retention: newest $KEEP survive.
ls -1t "$DEST"/chudware-web-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) |
  while read -r old; do rm -f "$old"; done

echo "backup ok: $out ($(du -h "$out" | cut -f1)) — $(ls "$DEST" | wc -l) kept"
