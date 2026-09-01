#!/bin/bash
# Разовый перенос баз из папки ./data (старый вариант с bind-mount или локальная разработка)
# в Docker-том affbazaar-data. Файлы в ./data не удаляются.
source "$(dirname "$0")/common.sh"
SRC="${SRC:-./data}"
[ -f "$SRC/bot.db" ] || [ -f "$SRC/site.db" ] || die "в $SRC нет bot.db/site.db"

if running bot; then
    yellow "! Бот работает — останавливаю на время импорта"
    $COMPOSE stop bot; RESTART=1
fi

$COMPOSE run --rm --no-deps -T -v "$(cd "$SRC" && pwd):/import:ro" bot sh -c '
set -e
cd /app/data
for db in bot site; do
    [ -f "/import/$db.db" ] || continue
    # копируем через sqlite backup API — так подтянется и содержимое WAL
    python -c "
import sqlite3, sys
src = sqlite3.connect(\"file:\" + sys.argv[1] + \"?mode=ro\", uri=True); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
src.close(); dst.close()" "/import/$db.db" "$db.db.new"
    rm -f "$db.db" "$db.db-wal" "$db.db-shm"; mv "$db.db.new" "$db.db"
    echo "  $db.db → том affbazaar-data"
done
for d in logs logs-restricted; do
    [ -d "/import/$d" ] && cp -r "/import/$d" . && echo "  $d/ → том affbazaar-data"
done
'
[ "${RESTART:-}" = 1 ] && $COMPOSE up -d bot
green "✔ данные перенесены в том affbazaar-data"
