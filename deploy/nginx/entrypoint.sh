#!/bin/sh
# Точка входа контейнера nginx.
#   nginx-entrypoint          — отрендерить конфиг сайта и запустить nginx
#   nginx-entrypoint reload   — перерендерить и перечитать конфиг (вызывает make ssl / make nginx)
# Пока сертификата нет — берётся шаблон http.conf, после make ssl — https.conf.
set -e

DOMAIN="${DOMAIN:-affbazaar.com}"
WWW="${WWW:-1}"
SRC=/etc/nginx/templates-src
OUT=/etc/nginx/conf.d/default.conf
CERT="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"

render() {
    if [ -f "$CERT" ]; then tpl=https; else tpl=http; fi
    sed "s/__DOMAIN__/${DOMAIN}/g" "$SRC/$tpl.conf" > "$OUT.tmp"
    if [ "$tpl" = "https" ] && [ "$WWW" != "1" ]; then
        # сертификат без www — убираем 443-блок для www, иначе nginx не стартует
        sed -i '/# --- www -> apex/,/# --- \/www ---/d' "$OUT.tmp"
        sed -i "s/server_name ${DOMAIN} www.${DOMAIN};/server_name ${DOMAIN};/" "$OUT.tmp"
    fi
    mv "$OUT.tmp" "$OUT"
    echo "nginx: конфиг '$tpl' для ${DOMAIN} (www=${WWW})"
}

case "${1:-}" in
    reload)
        render
        nginx -t && nginx -s reload
        ;;
    *)
        render
        # раз в 6 часов перечитываем конфиг: подхватывает продлённый сертификат
        ( while :; do sleep 6h; render; nginx -t >/dev/null 2>&1 && nginx -s reload; done ) &
        exec nginx -g 'daemon off;'
        ;;
esac
