#!/bin/bash
# Создаёт .env для боевого сервера под $DOMAIN. Существующий .env не трогает.
source "$(dirname "$0")/common.sh"

if [ -f .env ]; then
    info ".env уже существует — не перезаписываю."
    info "Проверка: make check-env   Правка: nano .env"
    exit 0
fi

SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32)

cat > .env <<ENVEOF
# --- Домен и прокси (make caddy / docker-compose) ---
DOMAIN=${DOMAIN}
# 1 — сайт также на www.${DOMAIN} (нужна A-запись), 0 — только голый домен
WWW=${WWW}
# Порт бота на хосте (127.0.0.1) для проверок. 8081, т.к. 8080 занят проектом affbiz
BOT_PORT=${BOT_PORT}
# Docker-сеть, в которой живёт Caddy проекта affbiz (бот подключается к ней). make caddy поправит сам.
CADDY_NETWORK=${CADDY_NETWORK}
# Контейнер Caddy и его Caddyfile на хосте. Пусто = определить автоматически
CADDY_CONTAINER=
CADDYFILE=

# --- Telegram ---
BOT_TOKEN=
# Числовые Telegram ID админов через запятую (узнать: @userinfobot)
ADMINS=

# --- Веб-панель / сайт ---
WEB_HOST=0.0.0.0
WEB_PORT=8080
# Пароль запасного входа в /admin
ADMIN_PASSWORD=
# Секрет подписи cookie-сессий. Сгенерирован автоматически, менять на живом проекте не стоит.
SECRET_KEY=${SECRET}
# Публичный адрес сайта (HTTPS обязателен для Mini App)
PUBLIC_URL=https://${DOMAIN}
WEBAPP_URL=https://${DOMAIN}

# --- Оплата ---
# Пусто = Telegram Stars (XTR). Для фиата — provider_token из @BotFather.
PAYMENT_PROVIDER_TOKEN=

# --- Хранилище (в Docker переопределяется на /app/data) ---
DATA_DIR=
ENVEOF
chmod 600 .env

green "✔ .env создан для https://${DOMAIN}"
echo
echo "Заполните три поля и запускайте make deploy:"
echo "   BOT_TOKEN       — от @BotFather"
echo "   ADMINS          — ваш Telegram ID"
echo "   ADMIN_PASSWORD  — пароль в /admin"
echo
echo "   nano .env"
