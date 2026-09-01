#!/bin/bash
# Бэкап данных из Docker-тома affbazaar-data: bot.db и site.db снимаются через sqlite backup API
# (безопасно при WAL и работающем боте), плюс логи. Один архив на запуск, хранится 30 дней.
source "$(dirname "$0")/common.sh"
DAY=$(date +%F_%H%M)
OUT="$BACKUP_DIR/affbazaar-$DAY.tar.gz"
# sudo нужен только если папка бэкапов недоступна текущему пользователю (например /var/backups)
mkdir -p "$BACKUP_DIR" 2>/dev/null || $SUDO mkdir -p "$BACKUP_DIR"
[ -w "$BACKUP_DIR" ] && SUDO=""

if running bot; then RUN="$COMPOSE exec -T bot"; else RUN="$COMPOSE run --rm --no-deps -T bot"; fi

$RUN sh -c '
set -e
cd /app/data
rm -rf .backup && mkdir .backup
for db in bot site; do
    [ -f "$db.db" ] && python -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
src.close(); dst.close()" "$db.db" ".backup/$db.db"
done
dirs=""; [ -d logs ] && dirs="$dirs logs"; [ -d logs-restricted ] && dirs="$dirs logs-restricted"
tar czf - -C .backup . $( [ -n "$dirs" ] && echo "-C /app/data $dirs" )
rm -rf .backup
' | $SUDO tee "$OUT" >/dev/null

[ -s "$OUT" ] || { $SUDO rm -f "$OUT"; die "архив пустой — бэкап не удался"; }
$SUDO find "$BACKUP_DIR" -type f -name 'affbazaar-*.tar.gz' -mtime +30 -delete
green "✔ бэкап: $OUT ($(du -h "$OUT" | cut -f1))"
echo "  внутри: bot.db, site.db, logs/, logs-restricted/. Восстановить: make restore FILE=$OUT"
