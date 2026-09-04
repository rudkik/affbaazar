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

DOMAIN="${DOMAIN:-$(env_get DOMAIN)}";       DOMAIN="${DOMAIN:-affbazaar.com}"
WWW="${WWW:-$(env_get WWW)}";                WWW="${WWW:-1}"
BOT_PORT="${BOT_PORT:-$(env_get BOT_PORT)}"; BOT_PORT="${BOT_PORT:-8081}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/affbazaar}"
COMPOSE="docker compose"

# Caddy соседнего проекта. Пусто = определить автоматически (make caddy найдёт контейнер caddy).
CADDY_NETWORK="${CADDY_NETWORK:-$(env_get CADDY_NETWORK)}";     CADDY_NETWORK="${CADDY_NETWORK:-affbiz_default}"
CADDY_CONTAINER="${CADDY_CONTAINER:-$(env_get CADDY_CONTAINER)}"
CADDYFILE="${CADDYFILE:-$(env_get CADDYFILE)}"

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

# Все ли переданные IPv4 принадлежат прокси Cloudflare. all_cloudflare_ips IP [IP...]
# Диапазоны — https://www.cloudflare.com/ips-v4 (свежий список подтягивается, при недоступности — встроенный).
all_cloudflare_ips() {
    local ranges
    ranges=$(curl -fsS --max-time 5 https://www.cloudflare.com/ips-v4 2>/dev/null || true)
    [ -n "$ranges" ] || ranges="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18
108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15
104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"
    RANGES="$ranges" python3 - "$@" <<'PY'
import ipaddress, os, sys
nets = [ipaddress.ip_network(r) for r in os.environ["RANGES"].split()]
ips = [ipaddress.ip_address(a) for a in sys.argv[1:]]
sys.exit(0 if ips and all(any(ip in n for n in nets) for ip in ips) else 1)
PY
}

# Запущен ли сервис compose. running SERVICE
running() { $COMPOSE ps --status running --services 2>/dev/null | grep -qx "$1"; }

# Контейнер Caddy: из .env, иначе первый запущенный контейнер, у которого caddy в имени образа или контейнера.
# Пусто — контейнера нет (возможно, Caddy стоит службой на хосте).
find_caddy_container() {
    if [ -n "$CADDY_CONTAINER" ]; then echo "$CADDY_CONTAINER"; return; fi
    docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null \
      | awk 'tolower($2) ~ /caddy/ || tolower($1) ~ /caddy/ {print $1; exit}'
}
