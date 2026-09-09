# Подключение CryptoPay: оплата и пополнение баланса

Инструкция для разработчика (или Claude) в проекте, который принимает оплату через CryptoPay.
Процессинг уже развёрнут и работает по адресу `https://ubaduba.top`. Ниже всё, что нужно для интеграции.

## 1. Что это и как работает

CryptoPay принимает USDT и USDC в сетях Ethereum (ERC-20), BNB Smart Chain (BEP-20) и Tron (TRC-20).
Только приём: никаких выводов и переводов через API нет.

Схема одна и для оплаты заказа, и для пополнения баланса:

1. Ваш бэкенд создаёт **счёт** (`invoice`) на сумму в долларах через API.
2. Пользователя отправляете на `payment_url` (готовая страница оплаты с QR, адресом, выбором сети и таймером).
3. Когда платёж подтверждён в блокчейне, CryptoPay шлёт **вебхук** `invoice.paid` на ваш `webhook_url`.
4. Ваш бэкенд проверяет подпись вебхука и зачисляет деньги пользователю (или помечает заказ оплаченным).

Пополнение баланса отличается от оплаты только тем, что в счёте передаётся `customer_id` (id вашего пользователя),
а по вебхуку вы прибавляете `amount_confirmed` к его балансу.

## 2. Что нужно получить от администратора CryptoPay

В админке `https://ubaduba.top` (раздел **Services**) создаётся сервис для вашего проекта. Вам нужны три вещи:

| Что | Вид | Куда положить |
|-----|-----|---------------|
| API-ключ | `cp_live_…` | `CRYPTOPAY_API_KEY` в `.env` вашего проекта |
| Webhook secret | длинная случайная строка | `CRYPTOPAY_WEBHOOK_SECRET` |
| Базовый URL | `https://ubaduba.top` | `CRYPTOPAY_BASE_URL` |

И наоборот, администратору нужно сообщить ваш `webhook_url`: публичный `https://` адрес вашего эндпоинта,
например `https://your-site.com/webhooks/cryptopay`. Он вписывается в карточку сервиса в админке.
Приватные адреса (localhost, 192.168.x.x) на боевом сервере не принимаются.

Ключ показывается один раз. В коде хранить только в переменных окружения, в git не коммитить.

## 3. API

Базовый URL: `https://ubaduba.top/api/v1`. Все запросы с заголовками:

```
Authorization: Bearer cp_live_...
Content-Type: application/json
Accept: application/json
```

Суммы везде передаются и возвращаются **строками** (`"100.50"`), не числами: не теряйте точность на float.
Полная спецификация: `https://ubaduba.top/openapi.yaml`, интерактивно: `https://ubaduba.top/swagger`,
описание: `https://ubaduba.top/docs`.

### 3.1 Создать счёт

`POST /api/v1/invoices`

```json
{
  "amount": "100",
  "external_id": "topup-12345",
  "description": "Пополнение баланса",
  "customer_id": "user-42",
  "customer_email": "user@example.com",
  "metadata": { "user_id": 42, "kind": "topup" },
  "success_url": "https://your-site.com/billing?paid=1",
  "cancel_url": "https://your-site.com/billing",
  "expires_in": 3600
}
```

| Поле | Обязательно | Описание |
|------|-------------|----------|
| `amount` | да | сумма в USD, строка. USDT и USDC считаются 1:1 к доллару |
| `external_id` | нет | ваш id заказа/пополнения. По нему потом искать счёт и дедуплицировать |
| `customer_id` | нет | id вашего пользователя. Возвращается в вебхуке, по нему зачисляете баланс |
| `currency` + `network` | нет | `USDT`/`USDC` и `ethereum`/`bsc`/`tron`. Если не передавать, сеть выберет сам плательщик на странице оплаты. Передавать можно только оба поля вместе |
| `success_url` / `cancel_url` | нет | куда вернуть пользователя со страницы оплаты |
| `expires_in` | нет | секунды до истечения счёта, по умолчанию 3600 |
| `metadata` | нет | любой JSON, вернётся в вебхуке как есть |

Рекомендуется заголовок `Idempotency-Key: <uuid>` на создание счёта: повтор запроса с тем же ключом вернёт тот же
счёт вместо создания второго.

Ответ `201`:

```json
{
  "data": {
    "id": "0192a…",
    "status": "pending",
    "is_paid": false,
    "amount": "100.000000",
    "amount_received": "0.000000",
    "amount_confirmed": "0.000000",
    "currency": null,
    "network": null,
    "address": null,
    "payment_url": "https://ubaduba.top/pay/0192a…",
    "external_id": "topup-12345",
    "customer_id": "user-42",
    "metadata": { "user_id": 42, "kind": "topup" },
    "expires_at": "2026-09-09T12:00:00+00:00"
  }
}
```

Пользователя отправляете на `payment_url`. Если `currency`+`network` были переданы, в ответе сразу есть `address`
и `qr_payload`, их можно показать на своей странице вместо hosted checkout.

### 3.2 Проверить счёт

`GET /api/v1/invoices/{id}`. Возвращает ту же структуру. Нужен для страницы «ожидание оплаты» (опрос раз в 5–10 с)
и как страховка, если вебхук не дошёл.

Статусы: `pending` → `confirming` (платёж увидели, ждём подтверждений) → `paid` / `overpaid`.
Также `partially_paid` (прислали меньше), `expired`, `cancelled`. Оплаченным считать только `is_paid == true`.

### 3.3 Прочее

- `POST /api/v1/invoices/{id}/cancel` отменить неоплаченный счёт.
- `GET /api/v1/invoices?external_id=topup-12345` найти счёт по своему id.
- `GET /api/v1/networks` какие сети и монеты сейчас доступны.

Ошибки приходят в виде `{"error": {"code": "…", "message": "…", "details": {…}}}`. Коды: `401 unauthenticated`,
`422 validation_error`, `409 invalid_state`, `429 rate_limited`, `503 wallet_not_configured` / `no_free_address`
(процессинг временно не может выдать адрес, повторить позже).

## 4. Вебхуки

CryptoPay делает `POST` на ваш `webhook_url` с JSON-телом:

```json
{
  "id": "delivery-uuid",
  "event": "invoice.paid",
  "created_at": "2026-09-09T11:05:00+00:00",
  "data": {
    "invoice": { …тот же объект счёта, что в API… },
    "reversal": null
  }
}
```

Заголовки:

| Заголовок | Значение |
|-----------|----------|
| `X-CryptoPay-Event` | имя события, дубль `event` |
| `X-CryptoPay-Delivery` | uuid доставки, дубль `id` |
| `X-CryptoPay-Timestamp` | unix-время в секундах |
| `X-CryptoPay-Signature` | `sha256=<hex>` |

### 4.1 Проверка подписи (обязательно)

```
signature = "sha256=" + hex(HMAC_SHA256(key = webhook_secret, message = timestamp + "." + raw_body))
```

- Считать по **сырому** телу запроса до любого парсинга JSON.
- Сравнивать константным по времени сравнением (`hash_equals`, `crypto.timingSafeEqual`).
- Отбрасывать доставки, у которых `timestamp` старше 5 минут (защита от повтора).
- При неверной подписи отвечать `401` и ничего не делать.

Пример на PHP:

```php
$raw = file_get_contents('php://input');
$ts  = $_SERVER['HTTP_X_CRYPTOPAY_TIMESTAMP'] ?? '';
$sig = $_SERVER['HTTP_X_CRYPTOPAY_SIGNATURE'] ?? '';
$expected = 'sha256=' . hash_hmac('sha256', $ts . '.' . $raw, $_ENV['CRYPTOPAY_WEBHOOK_SECRET']);
if (!hash_equals($expected, $sig) || abs(time() - (int) $ts) > 300) {
    http_response_code(401); exit;
}
$event = json_decode($raw, true);
```

Пример на Node:

```js
import { createHmac, timingSafeEqual } from 'node:crypto'
// раньше JSON-парсера: app.post('/webhooks/cryptopay', express.raw({ type: '*/*' }), handler)
const ts = req.header('X-CryptoPay-Timestamp') ?? ''
const sig = req.header('X-CryptoPay-Signature') ?? ''
const expected = 'sha256=' + createHmac('sha256', process.env.CRYPTOPAY_WEBHOOK_SECRET).update(`${ts}.${req.body}`).digest('hex')
const ok = expected.length === sig.length && timingSafeEqual(Buffer.from(expected), Buffer.from(sig))
if (!ok || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return res.sendStatus(401)
const event = JSON.parse(req.body.toString('utf8'))
```

### 4.2 События и что делать

| Событие | Что делать |
|---------|-----------|
| `invoice.paid`, `invoice.overpaid` | зачислить `data.invoice.amount_confirmed` пользователю `data.invoice.customer_id` (или пометить заказ оплаченным). При `overpaid` зачислять фактическую сумму, она больше `amount` |
| `invoice.partially_paid` | счёт истёк, а прислали меньше. Решение ваше: зачислить `amount_confirmed` как частичное пополнение или считать неоплаченным |
| `invoice.reversed` | реорганизация блокчейна забрала уже подтверждённый платёж. Обязательно: списать обратно то, что зачислили по этому счёту, сумма в `data.reversal.amount` |
| `invoice.confirming` | платёж увидели, ещё не подтверждён. Можно показать пользователю «ожидаем подтверждения» |
| `invoice.expired`, `invoice.cancelled` | закрыть ожидание на своей стороне |

Правила обработки:

1. **Отвечать 2xx быстро** (до 10 секунд), тяжёлую работу класть в очередь. Не 2xx означает повтор: через 1 мин, 5 мин,
   30 мин, 2 ч, 6 ч, 24 ч.
2. **Идемпотентность.** Доставки могут дублироваться и приходить не по порядку. Хранить обработанные `id` доставок и
   `invoice.id` оплаченных счетов; зачислять баланс по одному счёту только один раз.
3. **Доверять статусу, а не событию.** Решение принимать по `data.invoice.status` / `is_paid` из тела, а в спорных
   случаях перечитать счёт через `GET /api/v1/invoices/{id}`.
4. Вебхук-эндпоинт исключить из CSRF-защиты и авторизации сессии.

## 5. Минимальная схема данных на вашей стороне

Таблица `crypto_topups` (или `payments`):

| Поле | Зачем |
|------|-------|
| `id` | ваш первичный ключ, он же `external_id` счёта |
| `user_id` | кому зачислять |
| `invoice_id` | id счёта в CryptoPay |
| `amount` | запрошенная сумма |
| `amount_credited` | сколько реально зачислено (после `paid` / `overpaid` / `partially_paid`) |
| `status` | `pending` / `paid` / `reversed` / `expired` / `cancelled` |
| `payment_url` | чтобы показать кнопку «продолжить оплату» |

Таблица `cryptopay_webhook_deliveries` с уникальным `delivery_id` для дедупликации.

## 6. Пользовательский сценарий пополнения

1. Страница «Пополнить баланс»: поле суммы (минимум, например, 5 USD, максимум по вашему усмотрению).
2. Бэкенд: создать запись `crypto_topups(status=pending)`, вызвать `POST /invoices` с
   `external_id = topup.id`, `customer_id = user.id`, `success_url` на страницу баланса. Сохранить `invoice_id`, `payment_url`.
3. Редирект пользователя на `payment_url`.
4. Вебхук `invoice.paid`: найти запись по `data.invoice.external_id` (или `invoice_id`), если ещё не `paid`:
   в одной транзакции обновить запись и прибавить `amount_confirmed` к балансу пользователя.
5. Страница баланса после возврата по `success_url`: показать «платёж обрабатывается», пока статус не `paid`
   (опрос своего бэкенда, который при необходимости перечитывает `GET /invoices/{id}`).
6. В истории пополнений показывать незавершённые счета с кнопкой «Оплатить» (`payment_url`), пока не `expires_at`.

## 7. SDK (необязательно)

В репозитории процессинга есть готовые клиенты с проверкой подписи: `sdk/php` (Composer, PHP 8.1+, есть Laravel
service provider и middleware `cryptopay.webhook`) и `sdk/js` (npm, Node 18+, `verifyWebhook`, middleware для
Express). Они не опубликованы в публичных реестрах: подключать как локальный пакет (`repositories: path` в
composer.json или `npm i ../cryptopay/sdk/js`). Если проще, реализуйте два HTTP-вызова и проверку подписи из
раздела 4 вручную, этого достаточно.

## 8. Проверка интеграции

1. Создать счёт через ваш интерфейс, убедиться, что редирект ведёт на `https://ubaduba.top/pay/…`.
2. Оплатить реальную небольшую сумму (например 1 USDT в Tron, там самая дешёвая комиссия).
3. Дождаться вебхука `invoice.paid`: в админке CryptoPay раздел **Webhooks** показывает доставки, ваш ответ и ошибки.
4. Убедиться, что баланс пользователя увеличился ровно один раз, даже если вебхук вручную повторить из админки (кнопка Retry).
5. Проверить, что запрос с неверной подписью получает `401` и ничего не меняет.
