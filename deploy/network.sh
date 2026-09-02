#!/bin/bash
# Сеть Caddy (по умолчанию affbiz_default) должна существовать до `docker compose up`, т.к. объявлена external.
source "$(dirname "$0")/common.sh"
if docker network inspect "$CADDY_NETWORK" >/dev/null 2>&1; then
    green "✔ сеть ${CADDY_NETWORK} существует"
else
    yellow "! сети ${CADDY_NETWORK} нет — создаю. Если Caddy живёт в другой сети, укажите её в .env: CADDY_NETWORK=имя"
    echo "   (посмотреть: docker network ls; сеть контейнера: docker inspect <caddy> -f '{{range \$k,\$v := .NetworkSettings.Networks}}{{\$k}} {{end}}')"
    docker network create "$CADDY_NETWORK" >/dev/null
fi
