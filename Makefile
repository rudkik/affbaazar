# ─────────────────────────────────────────────────────────────────────────────
#  Aff Bazaar — деплой на сервер: бот в Docker, наружу отдаёт Caddy проекта affbiz.
#  Caddy уже держит 80/443 и сам выпускает сертификаты, поэтому nginx/certbot не нужны:
#  бот подключается к Docker-сети affbiz, а в Caddyfile добавляется блок affbazaar.com.
#
#  Первый запуск:
#      make env            # создать .env → вписать BOT_TOKEN, ADMINS, ADMIN_PASSWORD
#      make deploy         # docker → сборка и запуск → блок в Caddyfile → проверка
#
#  Обновление после изменений в коде:
#      make update
#
#  Домен и параметры Caddy берутся из .env (DOMAIN, WWW, BOT_PORT, CADDY_NETWORK, CADDYFILE).
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN ?= $(strip $(shell sed -n 's/^DOMAIN=//p' .env 2>/dev/null))
ifeq ($(DOMAIN),)
DOMAIN := affbazaar.com
endif
WWW ?= $(strip $(shell sed -n 's/^WWW=//p' .env 2>/dev/null))
ifeq ($(WWW),)
WWW := 1
endif
BOT_PORT ?= $(strip $(shell sed -n 's/^BOT_PORT=//p' .env 2>/dev/null))
ifeq ($(BOT_PORT),)
BOT_PORT := 8081
endif
CADDY_NETWORK ?= $(strip $(shell sed -n 's/^CADDY_NETWORK=//p' .env 2>/dev/null))
ifeq ($(CADDY_NETWORK),)
CADDY_NETWORK := affbiz_default
endif
CADDYFILE       ?=
CADDY_CONTAINER ?=
BACKUP_DIR      ?= /var/backups/affbazaar
FILE            ?=
SRC             ?= ./data

COMPOSE := docker compose
SHELL   := /bin/bash
export DOMAIN WWW BOT_PORT CADDY_NETWORK CADDYFILE CADDY_CONTAINER BACKUP_DIR SRC

.DEFAULT_GOAL := help
.PHONY: help env check-env docker ports network build up down restart logs ps shell update \
        caddy caddy-snippet deploy health dns firewall backup restore import-data cron-backup test dev botfather

help: ## Список команд
	@echo "Домен: https://$(DOMAIN) (www=$(WWW))   бот: affbazaar-bot → 127.0.0.1:$(BOT_PORT), сеть Caddy: $(CADDY_NETWORK)"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── конфигурация ─────────────────────────────────────────────────────────────
env: ## Создать .env под боевой домен (SECRET_KEY генерируется сам)
	@bash deploy/env.sh

check-env: ## Проверить, что .env заполнен
	@bash deploy/check-env.sh

# ── docker ───────────────────────────────────────────────────────────────────
docker: ## Установить Docker, если его нет
	@command -v docker >/dev/null 2>&1 && echo "Docker уже установлен: $$(docker --version)" || \
	  { echo "Устанавливаю Docker…"; curl -fsSL https://get.docker.com | sh; }

ports: ## Проверить порты: 80/443 у Caddy, 127.0.0.1:BOT_PORT свободен
	@bash deploy/ports.sh

network: ## Убедиться, что сеть Caddy ($(CADDY_NETWORK)) существует
	@bash deploy/network.sh

build: ## Собрать образ бота
	$(COMPOSE) build

up: check-env network ## Собрать и запустить бота
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

down: ## Остановить бота (данные в томе остаются)
	$(COMPOSE) down

restart: ## Перезапустить бота (без пересборки)
	$(COMPOSE) restart
	@$(COMPOSE) ps

logs: ## Логи бота (Ctrl+C — выйти)
	$(COMPOSE) logs -f --tail=200

ps: ## Статус контейнера
	@$(COMPOSE) ps

shell: ## Shell внутри контейнера бота
	$(COMPOSE) exec bot bash

update: check-env network ## Обновить код (git pull, если есть репозиторий), пересобрать, проверить
	@if [ -d .git ]; then git pull --ff-only; else echo "Не git-репозиторий — пропускаю git pull"; fi
	$(COMPOSE) up -d --build
	@docker image prune -f >/dev/null
	@$(MAKE) --no-print-directory health

# ── caddy (проект affbiz) ────────────────────────────────────────────────────
caddy: ## Добавить/обновить блок $(DOMAIN) в Caddyfile проекта affbiz и перечитать Caddy
	@bash deploy/caddy.sh

caddy-snippet: ## Показать блок для Caddyfile (если вставлять руками)
	@sed -e "s/__DOMAIN__/$(DOMAIN)/g" -e "s/__UPSTREAM__/affbazaar-bot:8080/g" deploy/caddy/site.caddy

dns: ## Проверить, что DNS домена указывает на этот сервер
	@bash deploy/dns.sh

firewall: ## ufw: открыть 22, 80, 443
	@bash deploy/firewall.sh

# ── всё вместе ───────────────────────────────────────────────────────────────
deploy: docker check-env ports up caddy health botfather ## Полный первый деплой

health: ## Проверить контейнер, приложение, сеть Caddy, https://домен, сертификат, Mini App
	@bash deploy/health.sh

botfather: ## Что прописать в @BotFather после деплоя
	@echo
	@echo "── @BotFather ──────────────────────────────────────────────"
	@echo "  /newapp   → выбрать бота → Web App URL: https://$(DOMAIN)"
	@echo "  /setdomain → выбрать бота → $(DOMAIN)   (для Login Widget в браузере)"
	@echo "  Menu Button бот выставит сам при старте (WEBAPP_URL начинается с https://)"
	@echo "  В личке боту от аккаунта из ADMINS: /set_channel @ваш_канал"
	@echo "────────────────────────────────────────────────────────────"

# ── бэкапы ───────────────────────────────────────────────────────────────────
backup: ## Бэкап баз (sqlite backup API) и логов из тома в /var/backups/affbazaar
	@bash deploy/backup.sh

restore: ## Восстановить базы из архива: make restore FILE=/var/backups/affbazaar/affbazaar-<дата>.tar.gz
	@bash deploy/restore.sh "$(FILE)"

import-data: ## Разово перенести bot.db/site.db из ./data (или SRC=путь) в Docker-том
	@bash deploy/import-data.sh

cron-backup: ## Поставить ежедневный бэкап в /etc/cron.daily
	@bash deploy/cron-backup.sh

# ── разработка ───────────────────────────────────────────────────────────────
test: ## Прогнать тесты (tests/run.sh)
	@bash tests/run.sh

dev: ## Локальный запуск без Docker (venv)
	@[ -d venv ] || python3 -m venv venv
	@./venv/bin/pip install -q -r requirements.txt
	./venv/bin/python bot.py
