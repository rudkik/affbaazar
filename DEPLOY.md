# Деплой

Боевой домен: **https://affbazaar.com**. Бот и сайт работают в Docker. Наружу их отдаёт
**Caddy из соседнего проекта `affbiz`**, который уже держит порты 80/443 на этом сервере и сам
выпускает и продлевает сертификаты Let's Encrypt. Поэтому ни nginx, ни certbot не нужны:
бот подключается к Docker-сети `affbiz`, а в Caddyfile добавляется блок `affbazaar.com`.

```
интернет → Caddy (проект affbiz) :80/:443 ─┬→ affbazaar.com → affbazaar-bot:8080 → том affbazaar-data (bot.db, site.db, logs)
                                           └→ сайт affbiz   → его nginx:80 (наружу 8080) — не трогаем
```

Целевая ОС сервера: Ubuntu 22.04/24.04 или Debian 12 (нужен `sudo`).

## Быстрый старт

```bash
# 0. DNS: A-запись affbazaar.com (и www.affbazaar.com) → IP сервера. Без этого Caddy не выпустит сертификат.

sudo apt update && sudo apt install -y git make curl
git clone <ваш-репозиторий> /opt/affbazaar && cd /opt/affbazaar

make env          # создаст .env под https://affbazaar.com, SECRET_KEY сгенерирует сам
nano .env         # вписать BOT_TOKEN, ADMINS, ADMIN_PASSWORD

make deploy       # docker → сборка и запуск → блок в Caddyfile + reload Caddy → проверка
make cron-backup  # ежедневный бэкап баз в /var/backups/affbazaar
```

`make deploy` идемпотентен: если что-то упало (например, DNS ещё не разъехался), поправьте
и запустите снова. По шагам это `make docker check-env ports up caddy health`.

В конце `make deploy` выведет, что прописать в @BotFather (`/newapp` с Web App URL
`https://affbazaar.com`, `/setdomain`).

## Как бот подключается к Caddy

`make caddy` делает следующее, ничего не спрашивая:

1. Проверяет DNS домена (и `www`; если для `www` записи нет — пишет `WWW=0` в `.env` и не добавляет его).
2. Находит контейнер Caddy (первый с образом `caddy`; можно задать `CADDY_CONTAINER=` в `.env`).
3. Проверяет, что бот и Caddy в одной Docker-сети. По умолчанию это `affbiz_default`
   (сеть compose-проекта `affbiz`); если Caddy в другой — записывает её в `.env` как
   `CADDY_NETWORK=` и пересоздаёт контейнер бота в ней.
4. Находит Caddyfile на хосте по монтированию контейнера (или берёт `CADDYFILE=` из `.env`),
   делает его резервную копию и вставляет блок между маркерами `# --- affbazaar begin/end ---`.
   Повторный запуск обновляет блок, чужие сайты не трогает.
5. `caddy validate`, затем `caddy reload`. Если проверка не прошла — откатывает Caddyfile.

Сам блок (`make caddy-snippet`, если хочется вставить руками):

```caddyfile
www.affbazaar.com {
	redir https://affbazaar.com{uri} permanent
}
affbazaar.com {
	encode zstd gzip
	header { Strict-Transport-Security "max-age=31536000; includeSubDomains" ... }
	reverse_proxy affbazaar-bot:8080
}
```

Caddy сам ставит `X-Forwarded-For` и `X-Forwarded-Proto`, бот им доверяет и на https ставит
куки сессий с флагом `Secure`. `X-Frame-Options` намеренно не выставляется: Telegram Web
открывает Mini App в iframe.

Если Caddy стоит не в Docker, а службой на хосте (`systemctl status caddy`), `make caddy` сам
переключится на этот режим: правит `/etc/caddy/Caddyfile`, проксирует на `127.0.0.1:8081`
и делает `systemctl reload caddy`. Контейнер ищется по слову `caddy` в имени образа или
контейнера; если он назван иначе — `CADDY_CONTAINER=имя` в `.env`.

## Повседневные команды

| Команда | Что делает |
|---|---|
| `make update` | подтянуть код (`git pull`), пересобрать, перезапустить, проверить |
| `make logs` | логи бота в реальном времени |
| `make restart` / `make down` / `make up` | перезапуск / остановка / запуск бота |
| `make health` | контейнер, приложение, сеть с Caddy, `https://affbazaar.com`, редирект, сертификат, Mini App |
| `make caddy` | повторно вставить/обновить блок в Caddyfile и перечитать Caddy |
| `make caddy-snippet` | показать блок для Caddyfile |
| `make backup` | архив с `bot.db`, `site.db` и логами в `/var/backups/affbazaar` |
| `make restore FILE=…` | восстановить базы из такого архива |
| `make import-data` | разово перенести базы из `./data` в Docker-том |
| `make shell` | shell внутри контейнера бота |
| `make test` | прогон тестов |
| `make help` | список всех команд |

Параметры в `.env`: `DOMAIN=affbazaar.com`, `WWW=1`, `BOT_PORT=8081`, `CADDY_NETWORK=affbiz_default`,
`CADDY_CONTAINER=` и `CADDYFILE=` (пустые = автоопределение).

## Данные

База — SQLite, она работает внутри контейнера бота как библиотека, отдельного сервера нет.
Файлы лежат в Docker-томе `affbazaar_affbazaar-data` (внутри контейнера `/app/data`):

```
bot.db              рабочая база бота
site.db             независимая база-дублёр для сайта
logs/<chat_id>/     логи действий, logs/bot.log — технический лог
logs-restricted/    логи превысивших лимит
```

`make down` и пересборка данные не трогают, удаляет их только `docker compose down -v`.
Посмотреть файлы: `make shell` → `ls -la /app/data`. Достать наружу: `make backup`.
Если базы уже есть в папке `./data` (локальная разработка) — `make import-data` перенесёт их в том.

## Что где лежит

```
Makefile                  команды деплоя
docker-compose.yml        контейнер бота (проект affbazaar), сеть Caddy как external, порт 127.0.0.1:${BOT_PORT}
deploy/env.sh             генерация .env под домен
deploy/check-env.sh       проверка .env перед запуском (пустой токен, http вместо https и т.п.)
deploy/ports.sh           80/443 у Caddy, 127.0.0.1:BOT_PORT свободен
deploy/network.sh         сеть Caddy существует (иначе создаёт)
deploy/caddy.sh           вставка блока в Caddyfile проекта affbiz, validate, reload
deploy/caddy/site.caddy   шаблон блока для Caddyfile
deploy/caddy/insert.py    вставка блока между маркерами
deploy/dns.sh             проверка, что A-запись смотрит на этот сервер
deploy/health.sh          проверка после деплоя
deploy/backup.sh          архив баз (sqlite backup API, безопасно при WAL) и логов из тома
deploy/restore.sh         восстановление баз из архива
deploy/import-data.sh     перенос баз из ./data в том
deploy/cron-backup.sh     регистрация ежедневного бэкапа
deploy/firewall.sh        ufw
```

---

## Ручная установка (без make)

Всё, что делает `make`, можно повторить руками. Два варианта: **Docker** (рекомендуется)
и **systemd** на обычном Linux-хостинге. Оба ставят одно и то же: бот + веб-сервер в одном процессе.

## 0. Что нужно до деплоя

1. Токен бота у [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Ваш числовой Telegram ID (узнать: [@userinfobot](https://t.me/userinfobot)) — в `ADMINS`.
3. **Канал объявлений**, куда бот добавлен администратором. Нужны права:
   «Публиковать сообщения», «Закреплять сообщения», «Удалять сообщения» — без них не будут
   работать публикация, платный закреп и кнопки удаления под постом.
4. Сервер: Linux x86_64/arm64, 1 vCPU / 1 ГБ RAM хватает, открытый порт для веб-панели.

Если вы дополнительно используете **чат** с гейтом по подписке: в @BotFather `/setprivacy` →
**Disable** (иначе бот не видит обычные сообщения в чате), и бот должен быть админом чата
с правами удалять сообщения и блокировать участников. Для канала объявлений это не нужно.

---

## Вариант A. Docker (рекомендуется)

### Установка

```bash
# Docker, если его нет
curl -fsSL https://get.docker.com | sh

git clone <ваш-репозиторий> /opt/affbazaar   # или скопируйте папку
cd /opt/affbazaar

cp .env.example .env
nano .env        # BOT_TOKEN, ADMINS, ADMIN_PASSWORD, SECRET_KEY, PUBLIC_URL

docker compose up -d --build
docker compose logs -f
```

В логах бота должно появиться `Бот запущен: @ваш_бот` и `Веб-сервер: http://0.0.0.0:8080`.
Подключение к Caddy: `bash deploy/caddy.sh` (то же, что `make caddy`).

### Обязательно поменяйте в `.env`

```ini
BOT_TOKEN=123456:AA...            # от @BotFather
ADMINS=123456789                  # ваш Telegram ID, через запятую можно несколько
ADMIN_PASSWORD=длинный-пароль     # вход в /admin
SECRET_KEY=<32+ случайных байт>   # openssl rand -hex 32
PUBLIC_URL=https://affbazaar.com
WEB_PORT=8080
PAYMENT_PROVIDER_TOKEN=           # пусто = оплата Telegram Stars
```

### Данные

Всё пишется в Docker-том `affbazaar-data` (в контейнере `/app/data`): `bot.db`, `site.db`,
`logs/`, `logs-restricted/`. Пересборка образа и `docker compose down` данные не трогают.

### Команды

```bash
docker compose logs -f bot             # логи бота
docker compose restart                 # перезапуск
docker compose down                    # остановить
docker compose up -d --build           # обновить после git pull
docker compose exec bot python -c "print('ok')"
```

---

## Вариант B. systemd (без Docker)

Нужен Python 3.10+.

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

git clone <ваш-репозиторий> /opt/affbazaar
cd /opt/affbazaar
./start.sh          # спросит BOT_TOKEN, ADMINS, порт, пароль админки и создаст службу
```

Скрипт создаёт venv, ставит зависимости, генерирует `.env` (с случайным `SECRET_KEY`)
и регистрирует службу с именем папки.

```bash
sudo systemctl status affbazaar     # статус
sudo journalctl -u affbazaar -f     # логи
sudo systemctl restart affbazaar    # перезапуск
./delete.sh                             # удалить службу
```

Обновление:

```bash
cd /opt/affbazaar && git pull
source venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart affbazaar
```

---

## nginx + HTTPS на хосте (запасной вариант без Caddy)

На боевом сервере это делает Caddy (`make caddy`). Раздел нужен только для systemd-варианта
или сервера без Caddy.

Веб-часть слушает `127.0.0.1:8081` (Docker) или `127.0.0.1:8080` (systemd). Наружу отдаём через nginx.

`/etc/nginx/sites-available/affbazaar`:

```nginx
server {
    listen 80;
    server_name affbazaar.com;

    location / {
        proxy_pass http://127.0.0.1:8081;   # 8080 для systemd-варианта
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # админка — по желанию ограничить по IP
    # location /admin { allow 1.2.3.4; deny all; proxy_pass http://127.0.0.1:8080; }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/affbazaar /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d affbazaar.com          # HTTPS + автопродление
```

После этого в `.env`: `PUBLIC_URL=https://affbazaar.com` и перезапустите бота.



---

## Mini App: сайт внутри бота

Сайт открывается прямо в Telegram как приложение, а пользователь на нём авторизован
автоматически — без паролей и регистрации.

**Как это устроено.** Telegram передаёт странице строку `initData`, подписанную секретом
вашего бота. Сервер проверяет подпись (`app/auth.py`) и выдаёт свою куку сессии. Подделать
её нельзя: без токена бота корректную подпись не собрать. Поэтому сайт доверяет только
проверенным данным и никогда — `user_id` из тела запроса.

Что получает вошедший: свой баланс коинов, список своих объявлений с их статусом и
комментариями модератора. Админ бота (тот, чей id указан в `ADMINS`) входит в `/admin`
через Telegram, пароль ему не нужен — пароль остаётся запасным входом.

Снаружи Telegram (обычный браузер) работает кнопка **Telegram Login Widget** — тот же
результат, но требует `/setdomain` в @BotFather.

### Обязательное условие

Telegram открывает Mini App **только по HTTPS с валидным сертификатом**. `http://localhost`
не подойдёт. Варианты:

- боевой: домен + nginx + certbot (см. раздел выше);
- быстрый тест без домена — туннель:
  ```bash
  # вариант 1
  cloudflared tunnel --url http://localhost:8080
  # вариант 2
  ngrok http 8080
  ```
  Полученный `https://…` адрес пропишите в `.env` как `PUBLIC_URL` и `WEBAPP_URL`
  и перезапустите бота.

### Что сделать в @BotFather

1. **Токен** — `/newbot` или `/token` для существующего бота. Положить в `.env` → `BOT_TOKEN`.
2. **Mini App** — `/newapp` → выбрать бота → название, описание, картинка 640×360,
   **Web App URL** = ваш `https://…` адрес → короткое имя.
   Получите прямую ссылку вида `t.me/ваш_бот/имя_приложения`.
3. **Кнопка «Приложение»** рядом со строкой ввода — бот ставит её сам при старте,
   если `WEBAPP_URL` начинается с `https://` (в логе будет `Mini App подключён: …`).
   Вручную: Bot Settings → Menu Button → Configure menu button → ваш URL.
4. **`/setdomain`** → выбрать бота → ваш домен (без пути). Нужно только для входа
   через Login Widget с обычного сайта; для Mini App не требуется.
5. **`/setprivacy` → Disable** — только если используете чат с гейтом по подписке.
   Для канала объявлений не нужно.

### Что прописать в `.env`

```ini
BOT_TOKEN=123456:AA...
ADMINS=ваш_telegram_id
PUBLIC_URL=https://affbazaar.com
WEBAPP_URL=https://affbazaar.com     # можно не задавать — возьмётся PUBLIC_URL
SECRET_KEY=<openssl rand -hex 32>           # им подписываются куки сессий
ADMIN_PASSWORD=<запасной вход в админку>
```

`SECRET_KEY` менять на живом проекте не стоит — все выданные сессии сразу перестанут
работать, и всем придётся войти заново.

### Проверка

1. В логе при старте: `Mini App подключён: https://…`.
2. Открыть бота → кнопка «Aff Bazar» слева от строки ввода → сайт открывается внутри
   Telegram, в шапке видно ваш `@username` и баланс.
3. Вкладка «Мои объявления» показывает только ваши.
4. С аккаунта из `ADMINS` открыть `/admin` — должна открыться панель без запроса пароля.
5. `curl -s https://ваш-домен/api/me` без куки → `{"authorized": false}`.

---

## Оплата токенов

- **Telegram Stars** (по умолчанию): `PAYMENT_PROVIDER_TOKEN` пустой, валюта `XTR`.
  Ничего подключать не нужно, работает сразу. Возврат: `/refund <charge_id>`
  (charge_id виден в админке во вкладке «Платежи»).
- **Классический биллинг**: в @BotFather → Payments → выбрать провайдера, получить токен,
  положить в `PAYMENT_PROVIDER_TOKEN`. Валюта переключится на `RUB`, суммы считаются в копейках.

Пакеты меняются в админке (`token_packages`), формат:
`[{"stars": 50, "tokens": 50}, {"stars": 100, "tokens": 120}]`.

---

## Бэкапы

`make backup` / `make cron-backup` / `make restore` делают это сами (данные в томе).
Ниже — вариант для systemd-установки, где базы лежат в папке на диске.

Базы SQLite в режиме WAL — копировать нужно средствами sqlite, а не `cp`.

`/etc/cron.daily/affbazaar-backup`:

```bash
#!/bin/bash
SRC=/opt/affbazaar/data
DST=/var/backups/affbazaar
mkdir -p "$DST"
DAY=$(date +%F)
sqlite3 "$SRC/bot.db"  ".backup '$DST/bot-$DAY.db'"
sqlite3 "$SRC/site.db" ".backup '$DST/site-$DAY.db'"
tar czf "$DST/logs-$DAY.tar.gz" -C "$SRC" logs logs-restricted
find "$DST" -mtime +30 -delete
```

```bash
sudo chmod +x /etc/cron.daily/affbazaar-backup
```

Восстановление: остановить бота, положить файлы обратно, запустить (в Docker — `make restore FILE=…`).

---

## Первичная настройка бота

В личке боту, с аккаунта из `ADMINS`:

1. `/set_channel @ваш_канал` — бот проверит, что он там администратор, и запомнит канал.
2. «📢 Канал и цены» — задать цену объявления, доплату за картинку и за закрепы 4/8 часов.
3. «⚙️ Глобальные настройки» → «📜 Текст правил» — свои правила публикации.
   Меняете правила существенно — увеличьте `rules_version`, тогда все примут их заново.
4. Вкладка «Рубрики» в веб-админке — при необходимости поправить названия, хэштеги, порядок
   и то, у каких рубрик спрашивать вертикаль.

## Проверка после установки

1. `curl -s localhost:8080/api/chats` → `[]` или список; `curl -s localhost:8080/api/rubrics`
   → 16 типов и 20 вертикалей.
2. Открыть `PUBLIC_URL/admin`, войти по `ADMIN_PASSWORD`.
3. В личке боту `/start` → меню; у админа `/admin` → панель.
4. С обычного аккаунта, **не подписанного** на канал: `/post` → должны показаться правила,
   после принятия — требование подписаться.
5. Подписаться, нажать «✅ Я подписался» → начислятся коины за подписку.
6. Пройти сценарий до конца с закрепом на 4 часа → пост появится в канале с хэштегами,
   закрепится, спишутся коины, и он же появится на сайте.
7. Нажать под постом «💬 Удалить с комментом», написать боту комментарий → пост исчезнет
   из канала и с сайта, автору вернутся коины и придёт комментарий.
8. Проверить файлы: `data/logs/<id канала>/<дата>.log`.

---

## Траблшутинг

| Симптом | Причина и решение |
|---|---|
| Бот не реагирует на сообщения в чате | Не выключен privacy mode: @BotFather → `/setprivacy` → Disable, затем **удалить и заново добавить** бота в чат |
| Не удаляет сообщения | Бот не админ в чате или нет права «Удалять сообщения» |
| Всех считает неподписанными | Бот не добавлен в канал. Проверка канала логируется как warning в `logs/bot.log` |
| Не может ограничить юзера | Нет права «Блокировать участников»; админов чата Telegram ограничить не даёт (и бот их не проверяет) |
| `/admin` отдаёт форму входа по кругу | Неверный `ADMIN_PASSWORD`, либо cookie режется прокси — проверьте `proxy_set_header` и HTTPS |
| Лента пустая | В неё попадают только объявления, опубликованные после запуска бота |
| «Канал для объявлений не задан» | Выполните `/set_channel @канал` от аккаунта из `ADMINS` |
| Пост публикуется, но не закрепляется | У бота нет права «Закреплять сообщения» в канале; коины за закреп в этом случае не списываются |
| Кнопки под постом нажимает кто попало | Так и должно быть: Telegram показывает их всем, но бот выполняет действие только для админов канала и `ADMINS` |
| Изменили шаблон страницы, а сайт прежний | HTML кэшируется в памяти процесса — перезапустите бота (`docker compose restart`) |
| Счёт на оплату не выставляется | Для Stars `PAYMENT_PROVIDER_TOKEN` должен быть **пустым**; для фиата — валидный токен провайдера |
| `database is locked` | Не запускайте два экземпляра бота на одном томе/папке с данными |

Технический лог: `data/logs/bot.log`, а также `docker compose logs -f` /
`journalctl -u affbazaar -f`.
