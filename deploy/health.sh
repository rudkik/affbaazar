#!/bin/bash
# Проверяет: контейнер жив, приложение отвечает на 127.0.0.1:BOT_PORT, Caddy отдаёт сайт по HTTPS.
source "$(dirname "$0")/common.sh"
fail=0

STATE=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' affbazaar-bot 2>/dev/null || echo "нет контейнера")
case "$STATE" in
    running*) green "✔ affbazaar-bot: $STATE";;
    *) red "✖ affbazaar-bot: $STATE"; fail=1;;
esac

if curl -fsS --max-time 5 "http://127.0.0.1:${BOT_PORT}/api/chats" >/dev/null 2>&1; then
    green "✔ приложение отвечает: http://127.0.0.1:${BOT_PORT}/api/chats"
else
    red "✖ приложение не отвечает на 127.0.0.1:${BOT_PORT} — смотрите make logs"; fail=1
fi

CADDY=$(find_caddy_container)
if [ -n "$CADDY" ]; then
    green "✔ Caddy: контейнер ${CADDY}"
    NETS=$(docker inspect affbazaar-bot -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)
    echo " $NETS" | grep -q " ${CADDY_NETWORK} " && green "✔ бот в сети ${CADDY_NETWORK}" || { red "✖ бот не в сети ${CADDY_NETWORK} (сети: ${NETS})"; fail=1; }
elif systemctl is-active caddy >/dev/null 2>&1; then
    green "✔ Caddy: служба на хосте (проксирует на 127.0.0.1:${BOT_PORT})"
else
    yellow "! Caddy не найден ни контейнером, ни службой"
fi

if out=$(curl -fsS --max-time 15 "https://${DOMAIN}/api/me" 2>&1); then
    green "✔ домен: https://${DOMAIN}/api/me → $out"
    code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "http://${DOMAIN}/" 2>/dev/null || true)
    case "$code" in 301|308) green "✔ http → https редирект работает";; *) yellow "! http://${DOMAIN} ответил $code, ожидался 308";; esac
    if command -v openssl >/dev/null 2>&1; then
        EXP=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
        [ -n "$EXP" ] && echo "  сертификат действует до: $EXP (Caddy продлевает сам)"
    fi
else
    red "✖ https://${DOMAIN} не отвечает: $out"
    echo "   Если make caddy только что выполнен — подождите 30 секунд (выпуск сертификата) и повторите make health."
    echo "   Логи Caddy: docker logs ${CADDY:-<caddy>} --tail 50"
    fail=1
fi

if docker logs affbazaar-bot 2>&1 | grep -q "Mini App подключён"; then
    green "✔ Mini App: кнопка приложения выставлена"
elif docker logs affbazaar-bot 2>&1 | grep -q "WEBAPP_URL не HTTPS"; then
    red "✖ Mini App: WEBAPP_URL не https — проверьте .env"; fail=1
fi

exit $fail
