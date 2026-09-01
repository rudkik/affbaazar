#!/bin/bash
# Восстановление баз из архива make backup. Останавливает бота, заменяет bot.db/site.db, запускает.
#   make restore FILE=/var/backups/affbazaar/affbazaar-2026-09-02_0300.tar.gz
source "$(dirname "$0")/common.sh"
FILE="${FILE:-${1:-}}"
[ -n "$FILE" ] || die "укажите архив: make restore FILE=/var/backups/affbazaar/affbazaar-<дата>.tar.gz"
[ -f "$FILE" ] || die "файл не найден: $FILE"

tar tzf "$FILE" | grep -qE '^\./?(bot|site)\.db$' || die "в архиве нет bot.db/site.db — это не бэкап make backup"

yellow "! Текущие bot.db и site.db в томе будут заменены данными из $FILE"
read -r -p "Продолжить? [y/N] " ans; [ "$ans" = "y" ] || [ "$ans" = "Y" ] || die "отменено"

$COMPOSE stop bot
$COMPOSE run --rm --no-deps -T bot sh -c '
set -e
cd /app/data
rm -f bot.db bot.db-wal bot.db-shm site.db site.db-wal site.db-shm
tar xzf - --wildcards "./bot.db" "./site.db" 2>/dev/null || tar xzf - bot.db site.db
ls -la bot.db site.db
' < "$FILE"
$COMPOSE up -d bot
green "✔ восстановлено из $FILE, бот запущен"
