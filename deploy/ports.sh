#!/bin/bash
# Порты 80/443 должны быть свободны для контейнера nginx (частая причина: nginx/apache на хосте).
source "$(dirname "$0")/common.sh"
busy=0
for p in 80 443; do
    holder=$($SUDO ss -ltnpH "sport = :$p" 2>/dev/null | sed -nE 's/.*users:\(\("([^"]+)".*/\1/p' | sort -u | tr '\n' ' ')
    [ -n "$holder" ] || continue
    case "$holder" in
        *docker*) ;;   # наш же контейнер nginx
        *) red "✖ порт $p занят: $holder"; busy=1;;
    esac
done
if [ "$busy" = 1 ]; then
    echo "   Остановите хостовый веб-сервер, например: sudo systemctl disable --now nginx apache2"
    exit 1
fi
green "✔ порты 80 и 443 свободны"
