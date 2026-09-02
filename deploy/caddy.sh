#!/bin/bash
# Добавляет сайт affbazaar.com в Caddyfile соседнего проекта (Caddy в Docker) и перечитывает Caddy.
# Сертификат Caddy выпустит сам. Повторный запуск обновляет блок между маркерами, чужое не трогает.
#   make caddy                  — всё автоматически (ищет контейнер caddy, его Caddyfile и сеть)
#   make caddy CADDYFILE=/путь  — если Caddyfile определить не удалось
source "$(dirname "$0")/common.sh"

running bot || die "контейнер бота не запущен — сначала make up"

# --- DNS: без A-записи Caddy не выпустит сертификат ---
bash deploy/dns.sh || die "Поправьте DNS и повторите make caddy"
if [ "$WWW" = "1" ] && ! bash deploy/dns.sh "www.${DOMAIN}" >/dev/null 2>&1; then
    yellow "! www.${DOMAIN} не указывает на этот сервер — www в Caddy не добавляю (WWW=0 записан в .env)"
    WWW=0; env_set WWW 0
fi

# --- контейнер Caddy ---
CADDY=$(find_caddy_container)
[ -n "$CADDY" ] || die "не нашёл контейнер Caddy (docker ps). Укажите в .env: CADDY_CONTAINER=имя"
info "Caddy: контейнер ${CADDY}"

# --- общая сеть: бот и Caddy должны видеть друг друга по имени ---
CADDY_NETS=$(docker inspect "$CADDY" -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')
if ! echo " $CADDY_NETS" | grep -q " ${CADDY_NETWORK} "; then
    yellow "! Caddy не в сети ${CADDY_NETWORK} (его сети: ${CADDY_NETS})"
    FIRST=$(echo "$CADDY_NETS" | awk '{print $1}')
    yellow "  Записываю CADDY_NETWORK=${FIRST} в .env и пересоздаю контейнер бота в этой сети"
    env_set CADDY_NETWORK "$FIRST"; CADDY_NETWORK="$FIRST"
    $COMPOSE up -d bot
fi
docker exec "$CADDY" sh -c "getent hosts affbazaar-bot >/dev/null 2>&1 || nslookup affbazaar-bot >/dev/null 2>&1" \
    && green "✔ Caddy видит affbazaar-bot по сети ${CADDY_NETWORK}" \
    || yellow "! Caddy пока не резолвит affbazaar-bot — проверьте, что оба в сети ${CADDY_NETWORK}"

# --- Caddyfile на хосте: из .env или из монтирования контейнера ---
CADDYFILE_IN=$(docker inspect "$CADDY" -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}{{end}}' \
               | grep -i 'caddyfile$' | head -1 || true)
if [ -z "$CADDYFILE" ]; then
    CADDYFILE="${CADDYFILE_IN%%|*}"
fi
CADDYFILE_CONTAINER="${CADDYFILE_IN##*|}"; CADDYFILE_CONTAINER="${CADDYFILE_CONTAINER:-/etc/caddy/Caddyfile}"

if [ -z "$CADDYFILE" ] || ! $SUDO test -f "$CADDYFILE"; then
    red "✖ Caddyfile на хосте не найден (в контейнер он не примонтирован?)."
    echo "   Укажите путь: make caddy CADDYFILE=/opt/affbiz/Caddyfile   или добавьте в его Caddyfile блок:"
    echo; sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__UPSTREAM__/affbazaar-bot:8080/g" deploy/caddy/site.caddy
    exit 1
fi
info "Caddyfile: ${CADDYFILE} (в контейнере ${CADDYFILE_CONTAINER})"

# --- вставка блока между маркерами ---
SNIPPET=$(sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__UPSTREAM__/affbazaar-bot:8080/g" deploy/caddy/site.caddy)
if [ "$WWW" != "1" ]; then
    SNIPPET=$(printf '%s\n' "$SNIPPET" | sed '/# --- www -> apex ---/,/# --- \/www ---/d')
fi
BACKUP="${CADDYFILE}.bak-$(date +%F_%H%M%S)"
$SUDO cp "$CADDYFILE" "$BACKUP"
SNIP_FILE=$(mktemp); printf '%s\n' "$SNIPPET" > "$SNIP_FILE"
$SUDO python3 deploy/caddy/insert.py "$CADDYFILE" "$SNIP_FILE"
rm -f "$SNIP_FILE"

# --- проверка и перечитка ---
if docker exec "$CADDY" caddy validate --config "$CADDYFILE_CONTAINER" >/dev/null 2>&1; then
    docker exec "$CADDY" caddy reload --config "$CADDYFILE_CONTAINER" 2>&1 | grep -v "^$" || true
    $SUDO rm -f "$BACKUP"
    green "✔ Caddy перечитал конфиг: https://${DOMAIN} → affbazaar-bot:8080"
    echo "  Сертификат Caddy выпустит сам за ~10–30 секунд. Проверка: make health"
else
    red "✖ caddy validate не прошёл — откатываю Caddyfile из ${BACKUP}"
    docker exec "$CADDY" caddy validate --config "$CADDYFILE_CONTAINER" 2>&1 | tail -5
    $SUDO cp "$BACKUP" "$CADDYFILE"
    exit 1
fi
