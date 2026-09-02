#!/bin/bash
# Порты: 80/443 должен держать Caddy (контейнер), 127.0.0.1:BOT_PORT — свободен для бота.
source "$(dirname "$0")/common.sh"
fail=0

holder_of() { $SUDO ss -ltnpH "sport = :$1" 2>/dev/null | sed -nE 's/.*users:\(\("([^"]+)".*/\1/p' | sort -u | tr '\n' ' '; }

for p in 80 443; do
    h=$(holder_of "$p")
    case "$h" in
        *docker*|*caddy*) green "✔ порт $p: ${h}(Caddy)";;
        "") yellow "! порт $p свободен — Caddy не запущен? make caddy потребует работающий Caddy";;
        *) red "✖ порт $p занят: $h — ожидался Caddy. Хостовый nginx/apache надо выключить или перевести за Caddy"; fail=1;;
    esac
done

h=$(holder_of "$BOT_PORT")
case "$h" in
    "") green "✔ 127.0.0.1:${BOT_PORT} свободен для бота";;
    *docker*) green "✔ ${BOT_PORT} занят контейнером (наш бот уже запущен?)";;
    *) red "✖ порт ${BOT_PORT} занят: $h — задайте другой в .env: BOT_PORT=8082"; fail=1;;
esac
exit $fail
