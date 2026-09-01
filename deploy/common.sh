# Общие переменные для скриптов деплоя. Подключается через `source`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Значение переменной из .env (без кавычек и пробелов). env_get KEY
env_get() {
    [ -f .env ] || return 0
    # `|| true`: отсутствие ключа — не ошибка (иначе set -e уронит скрипт)
    { grep -E "^$1=" .env || true; } | head -1 \
      | sed -E 's/^[^=]*=//; s/\r$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}
# Записать/обновить переменную в .env. env_set KEY VALUE
env_set() {
    if grep -qE "^$1=" .env; then
        sed -i.bak -E "s|^$1=.*|$1=$2|" .env && rm -f .env.bak
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

DOMAIN="${DOMAIN:-$(env_get DOMAIN)}"; DOMAIN="${DOMAIN:-affbazaar.com}"
WWW="${WWW:-$(env_get WWW)}";          WWW="${WWW:-1}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/affbazaar}"
COMPOSE="docker compose"
CERT_PATH="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"   # путь внутри контейнеров nginx/certbot

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
info()   { printf '\033[36m▸ %s\033[0m\n' "$*"; }
die()    { red "✖ $*"; exit 1; }

apt_install() {
    $SUDO apt-get update -qq
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "$@"
}

# Запущен ли сервис compose. running SERVICE
running() { $COMPOSE ps --status running --services 2>/dev/null | grep -qx "$1"; }
# Есть ли сертификат (смотрим из контейнера nginx)
cert_exists() { $COMPOSE exec -T nginx test -f "$CERT_PATH" 2>/dev/null; }
