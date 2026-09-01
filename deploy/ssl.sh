#!/bin/bash
# Выпускает сертификат Let's Encrypt контейнером certbot (webroot через nginx) и переключает nginx
# на HTTPS. Повторный запуск безопасен: certbot пропустит выпуск, конфиг просто перечитается.
source "$(dirname "$0")/common.sh"

running nginx || die "контейнер nginx не запущен — сначала make up"

bash deploy/dns.sh || die "Поправьте DNS и повторите make ssl"

DOMAINS=(-d "$DOMAIN")
if [ "$WWW" = "1" ]; then
    if bash deploy/dns.sh "www.${DOMAIN}" >/dev/null 2>&1; then
        DOMAINS+=(-d "www.${DOMAIN}")
    else
        yellow "! www.${DOMAIN} не указывает на этот сервер — сертификат только на ${DOMAIN} (WWW=0 записан в .env)"
        WWW=0; env_set WWW 0
    fi
fi

EMAIL_OPT=(--register-unsafely-without-email)
[ -n "$CERTBOT_EMAIL" ] && EMAIL_OPT=(-m "$CERTBOT_EMAIL")

info "Запрашиваю сертификат: ${DOMAINS[*]}"
$COMPOSE run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
    "${DOMAINS[@]}" --agree-tos --non-interactive --keep-until-expiring "${EMAIL_OPT[@]}"

cert_exists || die "Сертификат не появился в ${CERT_PATH}"

# nginx читает WWW из окружения контейнера — если он поменялся, контейнер надо пересоздать
if [ "$WWW" != "$($COMPOSE exec -T nginx sh -c 'echo $WWW' 2>/dev/null)" ]; then
    $COMPOSE up -d nginx
else
    $COMPOSE exec -T nginx sh /usr/local/bin/nginx-entrypoint reload
fi

if $COMPOSE run --rm --entrypoint certbot certbot renew --dry-run --webroot -w /var/www/certbot -q; then
    green "✔ Автопродление сертификата работает (контейнер certbot, раз в 12 часов)"
else
    yellow "! certbot renew --dry-run не прошёл — проверьте: make logs SVC=certbot"
fi

green "✔ HTTPS включён: https://${DOMAIN}"
