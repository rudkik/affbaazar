#!/bin/bash

# Derive service name from the current folder's name.
SERVICE_NAME="$(basename "$PWD")"
SERVICE_FILE="${SERVICE_NAME}.service"

echo "Остановка службы ${SERVICE_FILE}..."
sudo systemctl stop "${SERVICE_FILE}"

echo "Отключение службы ${SERVICE_FILE}..."
sudo systemctl disable "${SERVICE_FILE}"

echo "Удаление файла службы /etc/systemd/system/${SERVICE_FILE}..."
sudo rm -f /etc/systemd/system/"${SERVICE_FILE}"

echo "Перезагрузка демона systemd..."
sudo systemctl daemon-reload

echo "Служба ${SERVICE_NAME} была удалена."