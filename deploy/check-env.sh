#!/bin/bash
# Проверяет, что .env есть и заполнен — чтобы не собрать контейнер с пустым токеном.
source "$(dirname "$0")/common.sh"

[ -f .env ] || die ".env не найден. Создайте: make env"

errors=0
need() {  # need VAR "описание"
    local v; v=$(env_get "$1")
    if [ -z "$v" ]; then red "  ✖ $1 пуст — $2"; errors=1; fi
}
bad() {   # bad VAR значение "описание"
    local v; v=$(env_get "$1")
    if [ "$v" = "$2" ]; then red "  ✖ $1=$2 — $3"; errors=1; fi
}

need BOT_TOKEN "токен от @BotFather"
need ADMINS "ваш Telegram ID"
need ADMIN_PASSWORD "пароль входа в /admin"
need SECRET_KEY "openssl rand -hex 32"
bad  ADMIN_PASSWORD "change-me" "смените пароль"
bad  SECRET_KEY "change-me-too" "сгенерируйте: openssl rand -hex 32"

TOKEN=$(env_get BOT_TOKEN)
if [ -n "$TOKEN" ] && ! echo "$TOKEN" | grep -qE '^[0-9]+:[A-Za-z0-9_-]{30,}$'; then
    red "  ✖ BOT_TOKEN не похож на токен Telegram (формат 123456:AA…)"; errors=1
fi

PUBLIC_URL=$(env_get PUBLIC_URL)
case "$PUBLIC_URL" in
    https://*) ;;
    *) red "  ✖ PUBLIC_URL=$PUBLIC_URL — для Mini App нужен https:// (ожидается https://${DOMAIN})"; errors=1;;
esac
[ "$PUBLIC_URL" = "https://${DOMAIN}" ] || yellow "  ! PUBLIC_URL=$PUBLIC_URL отличается от DOMAIN=${DOMAIN} — nginx будет обслуживать ${DOMAIN}"
[ -n "$(env_get DOMAIN)" ] || yellow "  ! DOMAIN не задан в .env — nginx возьмёт ${DOMAIN}"

[ "$errors" -eq 0 ] || { echo; die "Исправьте .env (nano .env) и повторите."; }
green "✔ .env в порядке: ${PUBLIC_URL} (DOMAIN=${DOMAIN}, WWW=${WWW})"
