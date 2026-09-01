#!/bin/bash
# Установка и запуск бота как systemd-службы (без Docker).
set -e

cd "$(dirname "$0")"

# 1. Виртуальное окружение
if [ ! -d "venv" ]; then
    echo "Создаём виртуальное окружение..."
    python3 -m venv venv
fi
source venv/bin/activate

# 2. Конфигурация
if [ ! -f .env ]; then
    echo "Файл .env не найден. Заполните конфигурацию:"
    read -p "BOT_TOKEN: " BOT_TOKEN_INPUT
    read -p "ADMINS (Telegram ID админов бота через запятую): " ADMINS_INPUT
    read -p "Порт веб-панели [8080]: " WEB_PORT_INPUT
    read -p "Пароль веб-админки: " ADMIN_PASSWORD_INPUT
    read -p "Публичный адрес сайта [http://localhost:${WEB_PORT_INPUT:-8080}]: " PUBLIC_URL_INPUT

    cat > .env <<ENVEOF
BOT_TOKEN=${BOT_TOKEN_INPUT}
ADMINS=${ADMINS_INPUT}
WEB_HOST=0.0.0.0
WEB_PORT=${WEB_PORT_INPUT:-8080}
ADMIN_PASSWORD=${ADMIN_PASSWORD_INPUT}
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
PUBLIC_URL=${PUBLIC_URL_INPUT:-http://localhost:${WEB_PORT_INPUT:-8080}}
PAYMENT_PROVIDER_TOKEN=
DATA_DIR=
ENVEOF
    chmod 600 .env
    echo "Файл .env создан."
fi

# 3. Зависимости
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 4. systemd-служба
SERVICE_NAME="$(basename "$PWD")"
SERVICE_FILE="${SERVICE_NAME}.service"
WORKING_DIR="$(pwd)"
CURRENT_USER=$(whoami)

SERVICE_CONTENT="[Unit]
Description=Telegram chat-gate bot (${SERVICE_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${WORKING_DIR}
EnvironmentFile=${WORKING_DIR}/.env
ExecStart=${WORKING_DIR}/venv/bin/python3 ${WORKING_DIR}/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"

echo "Создание службы ${SERVICE_FILE}..."
echo "$SERVICE_CONTENT" | sudo tee /etc/systemd/system/"${SERVICE_FILE}" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_FILE}"
sudo systemctl restart "${SERVICE_FILE}"

echo
echo "Готово. Служба: ${SERVICE_NAME}"
echo "  логи:       sudo journalctl -u ${SERVICE_FILE} -f"
echo "  перезапуск: sudo systemctl restart ${SERVICE_FILE}"
echo "  статус:     sudo systemctl status ${SERVICE_FILE}"
