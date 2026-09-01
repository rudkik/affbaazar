#!/bin/bash
# ufw: SSH + HTTP + HTTPS. Порт бота наружу не публикуется вообще (только внутренняя сеть Docker).
source "$(dirname "$0")/common.sh"
command -v ufw >/dev/null 2>&1 || apt_install ufw
$SUDO ufw allow OpenSSH >/dev/null
$SUDO ufw allow 80/tcp  >/dev/null
$SUDO ufw allow 443/tcp >/dev/null
$SUDO ufw --force enable
$SUDO ufw status
