#!/bin/bash
# Проверяет, что A-запись домена (по умолчанию $DOMAIN) смотрит на публичный IP этого сервера.
source "$(dirname "$0")/common.sh"
HOST="${1:-$DOMAIN}"

MY_IP=$(curl -4 -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
     || curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
[ -n "$MY_IP" ] || die "Не удалось определить публичный IP сервера"

if command -v dig >/dev/null 2>&1; then
    RESOLVED=$(dig +short A "$HOST" @1.1.1.1 | grep -E '^[0-9.]+$' | sort -u | tr '\n' ' ')
else
    RESOLVED=$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')
fi

if echo " $RESOLVED" | grep -q " $MY_IP"; then
    green "✔ DNS: ${HOST} → ${MY_IP}"
else
    red "✖ DNS: ${HOST} → [${RESOLVED:-нет A-записи}], а сервер — ${MY_IP}"
    echo "   Добавьте A-запись ${HOST} → ${MY_IP} у регистратора и подождите обновления DNS."
    exit 1
fi
