#!/bin/bash
# Проверяет: контейнеры живы, приложение отвечает, домен отдаёт сайт по HTTPS.
source "$(dirname "$0")/common.sh"
fail=0

for c in affbazaar-bot affbazaar-nginx affbazaar-certbot; do
    STATE=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' "$c" 2>/dev/null || echo "нет контейнера")
    case "$STATE" in
        running*) green "✔ $c: $STATE";;
        *) red "✖ $c: $STATE"; fail=1;;
    esac
done

if $COMPOSE exec -T bot curl -fsS --max-time 5 http://127.0.0.1:8080/api/chats >/dev/null 2>&1; then
    green "✔ приложение отвечает (bot:8080/api/chats)"
else
    red "✖ приложение не отвечает — смотрите make logs SVC=bot"; fail=1
fi

if cert_exists; then
    if out=$(curl -fsS --max-time 10 "https://${DOMAIN}/api/me" 2>&1); then
        green "✔ домен: https://${DOMAIN}/api/me → $out"
    else
        red "✖ https://${DOMAIN} не отвечает: $out"; fail=1
    fi
    code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "http://${DOMAIN}/" 2>/dev/null || true)
    case "$code" in 301|308) green "✔ http → https редирект работает";; *) yellow "! http://${DOMAIN} ответил $code, ожидался 301";; esac
    if command -v openssl >/dev/null 2>&1; then
        EXP=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
        [ -n "$EXP" ] && echo "  сертификат действует до: $EXP"
    fi
else
    yellow "! сертификата нет — HTTPS ещё не включён (make ssl)"
fi

if docker logs affbazaar-bot 2>&1 | grep -q "Mini App подключён"; then
    green "✔ Mini App: кнопка приложения выставлена"
elif docker logs affbazaar-bot 2>&1 | grep -q "WEBAPP_URL не HTTPS"; then
    red "✖ Mini App: WEBAPP_URL не https — проверьте .env"; fail=1
fi

exit $fail
