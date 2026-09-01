# ─────────────────────────────────────────────────────────────────────────────
#  Aff Bazaar — деплой на сервер. Всё в Docker: бот+сайт, nginx, certbot.
#
#  Первый запуск на чистом сервере:
#      make env            # создать .env → вписать BOT_TOKEN, ADMINS, ADMIN_PASSWORD
#      make deploy         # docker → сборка и запуск → сертификат + HTTPS → проверка
#
#  Обновление после изменений в коде:
#      make update
#
#  Домен берётся из .env (DOMAIN=…), по умолчанию affbazaar.com.
#  Разово переопределить: make ssl DOMAIN=example.com WWW=0
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN ?= $(strip $(shell sed -n 's/^DOMAIN=//p' .env 2>/dev/null))
ifeq ($(DOMAIN),)
DOMAIN := affbazaar.com
endif
WWW ?= $(strip $(shell sed -n 's/^WWW=//p' .env 2>/dev/null))
ifeq ($(WWW),)
WWW := 1
endif
CERTBOT_EMAIL ?=
BACKUP_DIR    ?= /var/backups/affbazaar
SVC           ?=
FILE          ?=
SRC           ?= ./data

COMPOSE := docker compose
SHELL   := /bin/bash
export DOMAIN WWW CERTBOT_EMAIL BACKUP_DIR SRC

.DEFAULT_GOAL := help
.PHONY: help env check-env docker ports build up down restart logs ps shell update \
        nginx ssl certs deploy health dns firewall backup restore import-data cron-backup test dev botfather

help: ## Список команд
	@echo "Домен: https://$(DOMAIN) (www=$(WWW))   контейнеры: affbazaar-bot, affbazaar-nginx, affbazaar-certbot"
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

ports: ## Проверить, что порты 80/443 не заняты хостовым веб-сервером
	@bash deploy/ports.sh

build: ## Собрать образ бота
	$(COMPOSE) build

up: check-env ## Собрать и запустить все контейнеры (бот, nginx, certbot)
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

down: ## Остановить все контейнеры (данные и сертификаты остаются)
	$(COMPOSE) down

restart: ## Перезапустить (SVC=bot — только один сервис)
	$(COMPOSE) restart $(SVC)
	@$(COMPOSE) ps

logs: ## Логи (SVC=bot|nginx|certbot — только один сервис), Ctrl+C — выйти
	$(COMPOSE) logs -f --tail=200 $(SVC)

ps: ## Статус контейнеров
	@$(COMPOSE) ps

shell: ## Shell внутри контейнера бота
	$(COMPOSE) exec bot bash

update: check-env ## Обновить код (git pull, если есть репозиторий), пересобрать, проверить
	@if [ -d .git ]; then git pull --ff-only; else echo "Не git-репозиторий — пропускаю git pull"; fi
	$(COMPOSE) up -d --build
	@docker image prune -f >/dev/null
	@$(MAKE) --no-print-directory health

# ── nginx / https ────────────────────────────────────────────────────────────
nginx: ## Перечитать конфиг nginx после правки шаблонов в deploy/nginx/templates
	$(COMPOSE) exec -T nginx sh /usr/local/bin/nginx-entrypoint reload

ssl: ## Выписать сертификат Let's Encrypt и включить HTTPS
	@bash deploy/ssl.sh

certs: ## Показать сертификаты и сроки
	$(COMPOSE) run --rm --entrypoint certbot certbot certificates

dns: ## Проверить, что DNS домена указывает на этот сервер
	@bash deploy/dns.sh

firewall: ## ufw: открыть 22, 80, 443
	@bash deploy/firewall.sh

# ── всё вместе ───────────────────────────────────────────────────────────────
deploy: docker check-env ports up ssl health botfather ## Полный первый деплой на чистом сервере

health: ## Проверить контейнеры, приложение, https://домен, сертификат, Mini App
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
