#!/bin/bash
# Проверяет: контейнер жив, приложение отвечает на 127.0.0.1:BOT_PORT, Caddy отдаёт сайт по HTTPS.
source "$(dirname "$0")/common.sh"
fail=0

STATE=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' affbazaar-bot 2>/dev/null || echo "нет контейнера")
case "$STATE" in
    running*) green "✔ affbazaar-bot: $STATE";;
    *) red "✖ affbazaar-bot: $STATE"; fail=1;;
esac

# Повторяем попытки: try <секунд> <команда...> — сразу после make up бот поднимается несколько секунд
try() { local until=$(( $(date +%s) + $1 )); shift
        while :; do "$@" && return 0; [ "$(date +%s)" -ge "$until" ] && return 1; sleep 3; done; }

if try 30 curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:${BOT_PORT}/api/chats"; then
    green "✔ приложение отвечает: http://127.0.0.1:${BOT_PORT}/api/chats"
else
    red "✖ приложение не отвечает на 127.0.0.1:${BOT_PORT} (ждал 30 с) — смотрите make logs"; fail=1
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

# До 60 секунд: после make caddy сертификат выпускается не мгновенно
try 60 curl -fsS --max-time 10 -o /dev/null "https://${DOMAIN}/api/me" 2>/dev/null || true
if out=$(curl -fsS --max-time 15 "https://${DOMAIN}/api/me" 2>&1); then
    green "✔ домен: https://${DOMAIN}/api/me → $out"
    code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "http://${DOMAIN}/" 2>/dev/null || true)
    case "$code" in 301|308) green "✔ http → https редирект работает";; *) yellow "! http://${DOMAIN} ответил $code, ожидался 308";; esac
    if command -v openssl >/dev/null 2>&1; then
        ISSUER=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:443" 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null)
        EXP=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
        if echo "$ISSUER" | grep -qi cloudflare; then
            echo "  снаружи домен отдаёт сертификат Cloudflare (прокси), проверяю сертификат Caddy напрямую…"
            # Мимо прокси: резолвим домен в 127.0.0.1 — Caddy слушает 443 на этом сервере
            if curl -fsS --max-time 10 --resolve "${DOMAIN}:443:127.0.0.1" -o /dev/null "https://${DOMAIN}/api/me" 2>/dev/null; then
                OEXP=$(echo | openssl s_client -servername "$DOMAIN" -connect "127.0.0.1:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
                green "✔ сертификат Caddy на сервере валиден, действует до: ${OEXP:-?} (Caddy продлевает сам)"
            else
                yellow "! Caddy на 127.0.0.1:443 не отдаёт валидный сертификат для ${DOMAIN}"
                echo "   Если make caddy только что выполнен — подождите 30–60 с. Иначе в Cloudflare поставьте SSL/TLS «Full (strict)»"
                echo "   и проверьте, что запрос http://${DOMAIN}/.well-known/acme-challenge/ доходит до Caddy (порт 80 открыт: make firewall)."
            fi
        else
            [ -n "$EXP" ] && echo "  сертификат действует до: $EXP (Caddy продлевает сам)"
        fi
    fi
else
    red "✖ https://${DOMAIN} не отвечает: $out"
    echo "   Если make caddy только что выполнен — подождите 30–60 секунд (выпуск сертификата) и повторите make health."
    if [ -n "$CADDY" ]; then
        echo "   Логи Caddy: docker logs ${CADDY} --tail 50 | grep -i ${DOMAIN}"
    else
        echo "   Логи Caddy: journalctl -u caddy --since '15 min ago' --no-pager | grep -iE '${DOMAIN}|error' | tail -20"
    fi
    echo "   Блок в Caddyfile: grep -n ${DOMAIN} ${CADDYFILE:-/etc/caddy/Caddyfile}"
    fail=1
fi

if docker logs affbazaar-bot 2>&1 | grep -q "Mini App подключён"; then
    green "✔ Mini App: кнопка приложения выставлена"
elif docker logs affbazaar-bot 2>&1 | grep -q "WEBAPP_URL не HTTPS"; then
    red "✖ Mini App: WEBAPP_URL не https — проверьте .env"; fail=1
fi

exit $fail
