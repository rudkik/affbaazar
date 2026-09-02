#!/bin/bash
# Добавляет сайт affbazaar.com в Caddyfile соседнего проекта и перечитывает Caddy.
# Сертификат Caddy выпустит сам. Повторный запуск обновляет блок между маркерами, чужое не трогает.
#
# Два режима, определяются автоматически:
#   * Caddy в Docker  — бот и Caddy в одной сети, upstream affbazaar-bot:8080, reload через docker exec
#   * Caddy на хосте  — служба systemd, upstream 127.0.0.1:${BOT_PORT}, reload через systemctl
#
#   make caddy                       — всё автоматически
#   make caddy CADDY_CONTAINER=имя   — если контейнер не нашёлся сам
#   make caddy CADDYFILE=/путь       — если Caddyfile определить не удалось
source "$(dirname "$0")/common.sh"

running bot || die "контейнер бота не запущен — сначала make up"

# --- DNS: без A-записи Caddy не выпустит сертификат ---
bash deploy/dns.sh || die "Поправьте DNS и повторите make caddy"
if [ "$WWW" = "1" ] && ! bash deploy/dns.sh "www.${DOMAIN}" >/dev/null 2>&1; then
    yellow "! www.${DOMAIN} не указывает на этот сервер — www в Caddy не добавляю (WWW=0 записан в .env)"
    WWW=0; env_set WWW 0
fi

# --- где Caddy: контейнер или служба на хосте ---
CADDY=$(find_caddy_container)
MODE=""
if [ -n "$CADDY" ]; then
    MODE=docker
    info "Caddy: контейнер ${CADDY}"
elif command -v caddy >/dev/null 2>&1 || systemctl list-unit-files 2>/dev/null | grep -q '^caddy\.service'; then
    MODE=host
    info "Caddy: служба на хосте (systemd)"
else
    red "✖ Caddy не найден: ни контейнера с caddy в имени/образе, ни службы caddy на хосте."
    echo "   Посмотрите: docker ps --format '{{.Names}} {{.Image}}'   и   systemctl status caddy"
    echo "   Затем: make caddy CADDY_CONTAINER=<имя контейнера>   или   make caddy CADDYFILE=/путь/к/Caddyfile"
    exit 1
fi

# --- upstream и Caddyfile в зависимости от режима ---
if [ "$MODE" = "docker" ]; then
    UPSTREAM="affbazaar-bot:8080"

    # общая сеть: бот и Caddy должны видеть друг друга по имени
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

    # Caddyfile на хосте: из .env или из монтирования контейнера
    CADDYFILE_IN=$(docker inspect "$CADDY" -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}{{end}}' \
                   | grep -i 'caddyfile$' | head -1 || true)
    [ -n "$CADDYFILE" ] || CADDYFILE="${CADDYFILE_IN%%|*}"
    CADDYFILE_CONTAINER="${CADDYFILE_IN##*|}"; CADDYFILE_CONTAINER="${CADDYFILE_CONTAINER:-/etc/caddy/Caddyfile}"
    NOT_FOUND_HINT="в контейнер он не примонтирован? Укажите путь: make caddy CADDYFILE=/opt/affbiz/Caddyfile"
    validate() { docker exec "$CADDY" caddy validate --config "$CADDYFILE_CONTAINER"; }
    reload()   { docker exec "$CADDY" caddy reload   --config "$CADDYFILE_CONTAINER"; }
else
    UPSTREAM="127.0.0.1:${BOT_PORT}"
    [ -n "$CADDYFILE" ] || CADDYFILE=/etc/caddy/Caddyfile
    NOT_FOUND_HINT="укажите путь: make caddy CADDYFILE=/путь/к/Caddyfile"
    validate() { $SUDO caddy validate --config "$CADDYFILE" --adapter caddyfile; }
    reload()   { $SUDO systemctl reload caddy || $SUDO caddy reload --config "$CADDYFILE" --adapter caddyfile; }
fi

SNIPPET=$(sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__UPSTREAM__/${UPSTREAM}/g" deploy/caddy/site.caddy)
if [ "$WWW" != "1" ]; then
    SNIPPET=$(printf '%s\n' "$SNIPPET" | sed '/# --- www -> apex ---/,/# --- \/www ---/d')
fi

if [ -z "$CADDYFILE" ] || ! $SUDO test -f "$CADDYFILE"; then
    red "✖ Caddyfile на хосте не найден (${NOT_FOUND_HINT})."
    echo "   Или добавьте в его Caddyfile этот блок и перечитайте Caddy:"
    echo; printf '%s\n' "$SNIPPET"
    exit 1
fi
info "Caddyfile: ${CADDYFILE}  upstream: ${UPSTREAM}"

# --- вставка блока между маркерами (с резервной копией) ---
BACKUP="${CADDYFILE}.bak-$(date +%F_%H%M%S)"
$SUDO cp "$CADDYFILE" "$BACKUP"
SNIP_FILE=$(mktemp); printf '%s\n' "$SNIPPET" > "$SNIP_FILE"
$SUDO python3 deploy/caddy/insert.py "$CADDYFILE" "$SNIP_FILE"
rm -f "$SNIP_FILE"

# --- проверка и перечитка ---
if validate >/dev/null 2>&1; then
    reload 2>&1 | grep -v '^$' || true
    $SUDO rm -f "$BACKUP"
    green "✔ Caddy перечитал конфиг: https://${DOMAIN} → ${UPSTREAM}"
    echo "  Сертификат Caddy выпустит сам за ~10–30 секунд. Проверка: make health"
else
    red "✖ caddy validate не прошёл — откатываю Caddyfile из ${BACKUP}"
    validate 2>&1 | tail -5
    $SUDO cp "$BACKUP" "$CADDYFILE"
    exit 1
fi
