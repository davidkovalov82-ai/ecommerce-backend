# E-Commerce Backend

Модерен, модулен бекенд за електронна търговия, изграден с FastAPI, PostgreSQL, SQLAlchemy 2.0 и Alembic. Проектът използва `uv` като бърз мениджър на пакети и поддържа контейнеризация с Docker и Docker Compose.

> [!NOTE]
> За подробна техническа документация на текущата контролна точка, архитектурни диаграми, модел на данните, проследяване на сценариите и въпроси за техническо интервю вижте [docs/project-checkpoint.md](docs/project-checkpoint.md).

---

## Архитектура на проекта

Проектът се състои от три независими сервиса в `services/`:
- **`services/commerce`** – основният домейн бекенд (`accounts`, `catalog`, `cart`, `orders`, управление на складови резерви), издателят `commerce-outbox` (`order.paid`, `shipment.shipped` -> RabbitMQ) и консуматорът `commerce-payment-consumer` (`payment.*` -> `orders` + `order_payment_discrepancies`).
- **`services/payments`** – независим платежен сервис (`http://localhost:8001`) със собствена PostgreSQL база и роля (`payments`), собствени Alembic миграции, Stripe SDK интеграция, вътрешно HTTP API защитено с `X-Internal-Token`, публичен `/webhooks/stripe`, фонов издател `payments-outbox` и сверка `scripts/reconcile_payments.py`. Единствено `services/payments` получава `STRIPE_SECRET_KEY` и `STRIPE_WEBHOOK_SECRET`.
- **`services/notifications`** – независим консуматор на събития (`order.paid`, `shipment.shipped`) и worker за доставка по отделни канали (**Email** през SMTP/Mailpit и **SMS** през адаптер на провайдър), със собствена PostgreSQL база и роля (`notifications`).

В `services/commerce/app/modules/` са обособени следните домейни:

- **`catalog` (Продуктов каталог)**:
  - Управление и визуализация на активни продукти с пагинация (`limit`, `offset`).
  - Преглед на единичен продукт по ID (`GET /products/{id}`).
  - Ограничения на ниво база данни (Check constraints): неотрицателна цена (`price_minor >= 0`), неотрицателна наличност (`stock >= 0`), разрешена валута: само `EUR` (`ck_products_currency_eur`).
  - Публичният краен път скрива неактивните продукти (връща 404).
- **`accounts` (Потребителски профили и автентикация)**:
  - Регистрация на нови потребители (`POST /users`).
  - Сигурно хеширане на пароли с **Argon2id** чрез `pwdlib`.
  - Вход в системата (`POST /auth/login`) и издаване на кратковременни **JWT access tokens** (HS256, 15 минути).
  - Защита от timing attacks при грешен потребител чрез сравнение срещу предварително подготвен статичен dummy hash.
  - Извличане на текущо автентикирания потребител (`GET /users/me`) по `sub` claim от токена с директна проверка в базата данни.
  - Защита на паролите: пълно саниране и недопускане на изтичане на пароли във валидационните грешки (HTTP 422), логовете или HTTP отговорите.
  - Бизнес правила: роля по подразбиране `customer` (невъзможност за самоназначаване на административни роли), нормализация на имейли (case-insensitive уникалност).
- **`cart` (Потребителска количка)**:
  - Собственост и изолация: всеки регистриран потребител (и `customer`, и `admin`) управлява единствено своята собствена количка чрез проверения токен (`CurrentUserDep`). Заявките никога не приемат `user_id`.
  - Модел за данни: таблица `cart_items` със съставен първичен ключ `(user_id, product_id)`, каскадно изтриване при премахване на потребител (`ON DELETE CASCADE`), защита от изтриване на продукт в количка (`ON DELETE RESTRICT`) и валидация на количеството между 1 и 99 (`CHECK (quantity >= 1 AND quantity <= 99)`).
  - Идемпотентност и конкурентност: атомарен PostgreSQL upsert (`INSERT ... ON CONFLICT (user_id, product_id) DO UPDATE`) при задаване на количество.
  - Динамичност: цените не се замразяват в количката, а се изчисляват динамично от актуалния каталог. Количката не резервира и не намалява складова наличност (`stock`). При деактивиране или изчерпване на продукт позицията остава в количката с флаг `available: false`, но сумата ѝ продължава да участва в `total_minor`.
  - Оптимизация срещу N+1: групово зареждане на продуктите чрез една SQL заявка с `IN` клауза.

---

## Изисквания

- **Docker** и **Docker Compose** (препоръчително за най-лесно стартиране)
- *ИЛИ* локално:
  - **Python 3.13+**
  - **uv** (пакетен мениджър: https://docs.astral.sh/uv/)
  - **PostgreSQL 17**

---

## Бърз старт с Docker Compose

> [!IMPORTANT]
> Всички Docker Compose команди трябва да се изпълняват от **корена на хранилището**.

### 1. Подготовка на средата и генериране на тайни ключове (`JWT_SECRET`, `PAYMENTS_INTERNAL_AUTH_TOKEN`)

Преди първоначалното стартиране на контейнерите подгответе `.env` файловете (ако все още не съществуват) и генерирайте сигурни тайни:

Копирайте примерните конфигурации **без да презаписвате** съществуващи `.env` файлове:
```bash
test -f services/commerce/.env || cp services/commerce/.env.example services/commerce/.env
test -f services/payments/.env || cp services/payments/.env.example services/payments/.env
```

Генерирайте сигурни тайни (поне 32 байта):
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Задайте в `services/commerce/.env` и в средата (или root `.env` при `docker compose`):
- `JWT_SECRET` – тайна за подписване на JWT токени (поне 32 байта)
- `PAYMENTS_INTERNAL_AUTH_TOKEN` – споделен вътрешен токен между `commerce` и `payments` (задължителен, поне 16 байта)
- В `services/payments/.env` задайте `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` и същия `PAYMENTS_INTERNAL_AUTH_TOKEN`. *(Commerce не съхранява Stripe ключове).*

### 2. Стартиране на контейнерите (от корена на хранилището)

```bash
docker compose --env-file services/commerce/.env up --build
```

Compose автоматично:
1. Стартира PostgreSQL контейнер и инициализира три изолирани БД и роли (`commerce_app`, `payments`, `notifications`).
2. Стартира **RabbitMQ** (портове `5672` и `15672`) и **Mailpit** (SMTP `1025`, Web UI `http://localhost:8025`).
3. Построява и пуска **`api`** (`http://localhost:8000`), **`commerce-outbox`**, **`commerce-payment-consumer`**, **`payments`** (`http://localhost:8001`), **`payments-outbox`** и **`notifications`**.

### Преминаване от предишния запуск (`services/commerce`)

Всички Compose конфигурации са консолидирани в един основен `compose.yaml` файл в корена на хранилището (дублиращият файл в `services/commerce/` е премахнат).

Ако преди това сте стартирали проекта от директория `services/commerce`:
1. Спрете старите контейнери, **без** да изтривате постоянния volume:
   ```bash
   # ВАЖНО: Не използвайте 'down -v' или '--volumes', ако искате да запазите данните!
   docker rm -f commerce-db commerce-api
   # или ако се намирате в services/commerce (използвайки локалния .env път):
   docker compose --env-file .env down
   ```
2. Стартирайте приложението от **корена на хранилището**:
   ```bash
   docker compose --env-file services/commerce/.env up -d
   ```
   *Забележка:* В `compose.yaml` е конфигуриран проект `name: commerce`, което гарантира, че Docker продължава да използва същия именуван volume (`commerce_postgres_data`) без загуба на данни и без конфликти в имената на контейнерите.

### Зареждане на демонстрационни данни (Seeding)
За да заредите 100 примерни продукта в работещия контейнер (от корена на хранилището):

```bash
docker compose --env-file services/commerce/.env exec api python scripts/seed_products.py
```
*Забележка: Скриптът е идемпотентен – повторно изпълнение няма да дублира продуктите.*

### Повишаване на потребител до администратор (CLI)
Регистрацията през API създава потребители единствено с роля `customer`. Повишаването до `admin` се извършва контролирано през локална CLI команда:

```bash
# В работещия Docker контейнер (от корена на хранилището):
docker compose --env-file services/commerce/.env exec api python scripts/promote_admin.py admin@example.com

# Или локално (при стартиран само Postgres):
cd services/commerce && uv run python scripts/promote_admin.py admin@example.com
```

### Интерактивна документация и локална пощенска кутия
Отворете браузъра си на:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **OpenAPI JSON**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
- **Mailpit Web UI (пощенска кутия)**: [http://localhost:8025](http://localhost:8025)

---

## Локално разработване (без Docker за API)

Ако разработвате локално с Python и `uv`:

### 1. Конфигуриране на средата и генериране на JWT_SECRET

Първо подгответе `.env` файла, **без да презаписвате** съществуващ:
```bash
test -f services/commerce/.env || cp services/commerce/.env.example services/commerce/.env
```

Генерирайте сигурен таен ключ (поне 32 байта):
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Попълнете стойностите в `services/commerce/.env`:
```env
DB_HOST=localhost
DB_PORT=5432
DB_USER=commerce
DB_PASSWORD=commerce
DB_NAME=commerce

# Задължително: поне 32 байта таен ключ за JWT (няма работеща стойност по подразбиране)
JWT_SECRET=вашият_генериран_ключ_поне_32_байта
```

### 2. Стартиране на базата данни и Mailpit (от корена на хранилището)

```bash
docker compose --env-file services/commerce/.env up -d db mailpit
```

### 3. Инсталиране на зависимостите

```bash
cd services/commerce
uv sync
```

### 4. Изпълнение на база данни миграции (Alembic)

```bash
uv run alembic upgrade head
```

### 5. Зареждане на демонстрационни данни

```bash
uv run python scripts/seed_products.py
```

### 6. Стартиране на FastAPI сървъра

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 7. Независими сервиси `services/payments` и `services/notifications` през RabbitMQ (К4 / К5)

В К5 платежният домейн е напълно изнесен в `services/payments`. При `POST /orders/{order_id}/checkout-session` Commerce изчислява сумата от БД, освобождава DB блокировката и изпраща вътрешна HTTP заявка към Payments (`X-Internal-Token`), а Payments говори със Stripe API с детерминистичен `attempt_key` (идемпотентност при паралелни или изгубени заявки).

Входящите Stripe webhook събития се приемат **единствено от Payments** (`POST http://localhost:8001/webhooks/stripe`): Payments проверява криптографския подпис, записва опита и пише събитие (`payment.succeeded`, `payment.processing`, `payment.failed`, `payment.expired`) в своята таблица `payment_outbox_events`. Дори ако Commerce е спрян по време на плащането, Payments приема webhook-а, запазва резултата и го публикува в RabbitMQ exchange `payments.events` (`payments-outbox`). След възстановяване на Commerce `commerce-payment-consumer` валидира контракта, отбелязва поръчката като `paid` (през `order_inbox_events`) и атомарно записва `order.paid` в `order_outbox_events`, откъдето `commerce-outbox` и `notifications` изпращат точно по едно Email и SMS известие.

```mermaid
sequenceDiagram
    participant B as Покупка (Buyer)
    participant C as Commerce API (8000)
    participant P as Payments API (8001)
    participant S as Stripe
    participant RMQ as RabbitMQ
    participant CC as commerce-payment-consumer
    participant N as Notifications

    B->>C: POST /orders/{id}/checkout-session
    C->>C: Проверка под FOR UPDATE + commit
    C->>P: POST /internal/payments/checkout-sessions (X-Internal-Token)
    P->>S: Checkout Session Create (Idempotency-Key)
    S-->>P: session_id + checkout_url
    P-->>C: session_id + checkout_url
    C-->>B: checkout_url

    Note over S,P: Дори при спрян Commerce, Payments приема webhook-а
    S->>P: POST /webhooks/stripe (Stripe-Signature)
    P->>P: DB Tx: succeeded + payment_outbox_events (pending)
    P->>RMQ: payments-outbox publish payment.succeeded (payments.events)
    RMQ->>CC: deliver commerce.payment_events (DLQ при невалиден контракт)
    CC->>CC: DB Tx: order=paid + order_inbox_events + order_outbox_events (order.paid)
    CC->>RMQ: basic_ack след commit
    RMQ->>N: commerce-outbox -> commerce.events -> notifications.events
    N->>N: Email (Mailpit) & SMS (Twilio адаптер)
```

Стартиране на пълния стек (от корена на хранилището):
```bash
test -f services/commerce/.env || cp services/commerce/.env.example services/commerce/.env
test -f services/payments/.env || cp services/payments/.env.example services/payments/.env
docker compose --env-file services/commerce/.env up --build -d
```

- **Commerce API / Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Payments API / Swagger UI**: [http://localhost:8001/docs](http://localhost:8001/docs)
- **Mailpit Web UI**: [http://localhost:8025](http://localhost:8025)
- **RabbitMQ Management UI**: [http://localhost:15672](http://localhost:15672) (`guest` / `guest`)

> [!NOTE]
> **Изолация на три PostgreSQL роли и секрети**: В `compose.yaml` административната роля (`POSTGRES_ADMIN_USER`, по подразбиране `commerce` за запазване на `commerce_postgres_data`) инициализира 3 отделни бази и роли без суперпотребителски права: `commerce_app` (`commerce`), `payments` (`payments`) и `notifications` (`notifications`). Commerce няма `STRIPE_*` променливи и не може да чете таблиците на Payments или Notifications.
>
> **Разхождения (`order_payment_discrepancies`)**: Ако `payment.succeeded` пристигне след вече отказана (`cancelled`) или изтекла (`expired`) поръчка, Commerce не променя статуса, не пипа наличността и не публикува `order.paid`, а записва видимо разхождение в `order_payment_discrepancies` (достъпно в `GET /orders/{id}` чрез `has_payment_discrepancy` и `payment_discrepancies`).

#### Ръчно публикуване на Outbox, консумиране и пускане на трите сервиса локално

```bash
# Payments: миграции, API (8001) и outbox към RabbitMQ
cd services/payments && uv run alembic upgrade head && uv run uvicorn app.main:app --port 8001
cd services/payments && uv run python scripts/process_outbox.py --daemon

# Commerce: консуматор на payment.* и outbox издател за order.paid / shipment.shipped
cd services/commerce && uv run python scripts/consume_payment_events.py --daemon
cd services/commerce && uv run python scripts/process_outbox.py --daemon

# Notifications: Email/SMS worker
cd services/notifications && uv run alembic upgrade head && uv run python -m app.main
```

#### Безопасно преминаване (Cutover) от монолитен Commerce Stripe към К5 (`services/payments`)
1. Приложете миграцията в `services/commerce` (`uv run alembic upgrade head` до `g1b2c3d4e5f6` — добавя `active_attempt_key`, `order_inbox_events`, `order_payment_discrepancies` без да трие старите платежни таблици в Commerce).
2. Инициализирайте базата и ролята `payments` (`scripts/postgres_db_init.sh`, без изтриване на `commerce_postgres_data`) и приложете миграциите в `services/payments` (`uv run alembic upgrade head`).
3. Изпълнете повторяемия скрипт за пренос на старите отворени сесии, опити и обработени Stripe събития (може да се пусне многократно без дублиране):
   ```bash
   cd services/payments
   uv run python scripts/migrate_from_commerce.py \
     --commerce-url "postgresql+psycopg://postgres_or_reader:...@host:5432/commerce" \
     --payments-url "postgresql+psycopg://payments:...@host:5432/payments"
   ```
4. Стартирайте `payments`, `payments-outbox` и `commerce-payment-consumer`, след което пренасочете `stripe listen --forward-to http://localhost:8001/webhooks/stripe` към Payments (8001) и стартирайте обновения Commerce с конфигуриран `PAYMENTS_BASE_URL` и `PAYMENTS_INTERNAL_AUTH_TOKEN`.

---

## Тестване и проверка на кода

Проектът разполага с автоматизирани тестове в три независими сервиса (`commerce_test`, `payments_test` и `notifications_test`), както и с изолирани E2E пакети (`make test-k4-e2e` и `make test-k5-e2e`):

### Изпълнение на тестовете по сервиси

```bash
# 1. Commerce (lint, format, миграции и тестове):
cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pytest -q

# 2. Payments (lint, format, изолация на БД, конкурентност, webhooks, миграция от Commerce):
cd services/payments && uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pytest -q

# 3. Notifications (lint, format, контракти, DLQ, commit-преди-ack, Email/SMS):
cd services/notifications && uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pytest -q
```

### Изолирани E2E проверки от корена (`make test-k4-e2e`, `make test-k5-e2e`)

И двете команди вдигат отделен Docker Compose стек по `compose.e2e.yaml` на изолирани портове (`55432`, `55672`, `58000`, `58001`, `58025`, `58080`, `58081`), **без да спират работния Compose стек и без да трият неговите данни/volumes**:

```bash
make test-k4-e2e   # Сквозни K4 сценарии за Notifications (DLQ, broker outage, redelivery, multi-seller)
make test-k5-e2e   # Пълни K5 сценарии: Commerce спрян по време на webhook, срив на RabbitMQ, загубен Stripe отговор,
                   # паралелен checkout, async failed -> attempt_2 retry, cancel/discrepancy, сверки и migrate_from_commerce.py
```

### Проверка за форматиране и статичен анализ (Ruff)

```bash
uv run ruff check .
uv run ruff format --check .
```

Автоматично форматиране:
```bash
uv run ruff format .
```

---

## Основни API крайни точки

### 1. Каталог (Catalog)

- **`GET /products`** – Списък с активни продукти с пагинация:
  - Query параметри:
    - `limit` (int, default: 20, min: 1, max: 100)
    - `offset` (int, default: 0, min: 0)
  - Отговор:
    ```json
    {
      "items": [
        {
          "id": 1,
          "title": "Notebook",
          "price_minor": 1299,
          "currency": "EUR",
          "stock": 10,
          "active": true
        }
      ],
      "limit": 20,
      "offset": 0
    }
    ```

- **`GET /products/{product_id}`** – Детайли за продукт по ID:
  - Връща 200 с данните за продукта, ако е активен.
  - Връща 404 `PRODUCT_NOT_FOUND`, ако продуктът липсва или е деактивиран (`active: false`).

- **`POST /products`** – Създаване на нов продукт (само за администратори):
  - Изисква хедър: `Authorization: Bearer <admin_token>`
  - Тяло на заявката:
    ```json
    {
      "title": "Ergonomic Mechanical Keyboard",
      "price_minor": 12999,
      "currency": "EUR",
      "stock": 25,
      "active": true
    }
    ```
  - Изисквания към полетата:
    - `title`: низ от 1 до 255 символа (външните интервали се премахват автоматично).
    - `price_minor`: строго цяло неотрицателно число (от 0 до 2 147 483 647).
    - `currency`: само `EUR`.
    - `stock`: строго цяло неотрицателно число (от 0 до 2 147 483 647).
    - `active`: boolean, по подразбиране `true`.
    - Забранени са всякакви непознати полета, включително подаване на ръчно `id`.
  - Отговори:
    - `201 Created`: връща създадения продукт със системно генерирано `id`.
    - `401 Unauthorized`: при липсващ или невалиден Bearer токен.
    - `403 Forbidden` (`detail.code = "FORBIDDEN"`): при заявка от клиент (`customer`).
    - `422 Unprocessable Entity`: при нарушаване на типовете или ограниченията.

- **`PATCH /products/{product_id}`** – Частично обновяване на продукт (само за администратори):
  - Изисква хедър: `Authorization: Bearer <admin_token>`
  - Тяло на заявката (поне едно поле е задължително):
    ```json
    {
      "price_minor": 11999,
      "active": false
    }
    ```
  - Поведение:
    - Променят се само изрично подадените полета (`title`, `price_minor`, `currency`, `stock`, `active`). Пропуснатите запазват стойностите си.
    - Изрично `null`, празен обект `{}` и непознати полета (вкл. `id`) се отхвърлят с `422 Unprocessable Entity`.
    - Администраторът може да редактира и неактивни продукти.
    - При `active: false` продуктът се деактивира и веднага се скрива от публичните `GET` операции. При `active: true` се активира отново.
  - Отговори:
    - `200 OK`: връща актуализирания продукт с пълен `ProductResponse`.
    - `401 Unauthorized`: при липсващ или невалиден Bearer токен.
    - `403 Forbidden` (`detail.code = "FORBIDDEN"`): при заявка от клиент (`customer`).
    - `404 Not Found` (`detail.code = "PRODUCT_NOT_FOUND"`): при несъществуващ продукт.
    - `422 Unprocessable Entity`: при нарушаване на типовете, валидационните правила или празно/null тяло.

### 2. Потребителски профили и автентикация (Accounts & Auth)

- **`POST /users`** – Регистрация на нов клиент:
  - Тяло на заявката:
    ```json
    {
      "email": "customer@example.com",
      "password": "secure_password_15_chars_or_more"
    }
    ```
  - Изисквания:
    - Дължина на паролата: от 15 до 128 символа.
    - Имейл: валиден имейл адрес (автоматично trim и lowercase).
  - Отговор (201 Created):
    ```json
    {
      "id": 1,
      "email": "customer@example.com",
      "role": "customer"
    }
    ```
  - При опит за дублиран имейл: 409 Conflict (`EMAIL_ALREADY_EXISTS`).

- **`POST /auth/login`** – Вход в системата и получаване на JWT токен:
  - Тяло на заявката:
    ```json
    {
      "email": "customer@example.com",
      "password": "secure_password_15_chars_or_more"
    }
    ```
  - Поведение:
    - Имейлът се нормализира (trim и lowercase) по същите правила като при регистрацията.
    - Паролата се проверява директно в оригинален вид.
    - При несъществуващ имейл или грешна парола се връща унифициран отговор **401 Unauthorized** (`detail.code = "INVALID_CREDENTIALS"`, `detail.message = "Invalid email or password"`) с хедър `WWW-Authenticate: Bearer`.
    - Използва се предварително подготвен статичен dummy hash срещу timing attacks при неизвестен потребител.
  - Отговор (200 OK):
    ```json
    {
      "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
      "token_type": "bearer"
    }
    ```

- **`GET /users/me`** – Извличане на профила на текущо влезлия потребител:
  - Изисква хедър:
    ```http
    Authorization: Bearer <access_token>
    ```
  - Поведение:
    - Проверява подписа, валидността и срока на токена (HS256).
    - Извлича актуалния потребител от базата данни по `sub` (user ID) – ролята и правата не се четат от недоверени данни в токена.
    - При невалиден/изтекъл токен или изтрит потребител връща **401 Unauthorized** с хедър `WWW-Authenticate: Bearer`.
  - Отговор (200 OK):
    ```json
    {
      "id": 1,
      "email": "customer@example.com",
      "role": "customer"
    }
    ```

### 3. Потребителска количка (Cart)

Всички операции с количката са строго автентикирани (`CurrentUserDep`) и оперират единствено върху количката на текущия потребител.

- **`GET /cart`** – Преглед на текущата количка:
  - Изисква хедър: `Authorization: Bearer <token>`
  - Празна количка връща:
    ```json
    {
      "items": [],
      "total_minor": 0,
      "currency": "EUR"
    }
    ```
  - Непразна количка връща актуално изчислените позиции, сортирани по `product_id` възходящо:
    ```json
    {
      "items": [
        {
          "product_id": 1,
          "title": "Notebook",
          "quantity": 2,
          "unit_price_minor": 1299,
          "subtotal_minor": 2598,
          "available": true
        }
      ],
      "total_minor": 2598,
      "currency": "EUR"
    }
    ```
  - Полета на всяка позиция:
    - `product_id`: идентификатор на продукта;
    - `title`: актуално заглавие от каталога;
    - `quantity`: брой единици в количката (1–99);
    - `unit_price_minor`: актуална единична цена в центове;
    - `subtotal_minor`: произведение на `unit_price_minor * quantity`;
    - `available`: `true`, ако продуктът е активен (`active: true`) и наличността покрива заявеното количество (`stock >= quantity`). При изчерпване или деактивиране става `false`, но позицията остава в количката и сумата ѝ продължава да участва в `total_minor`.

- **`PUT /cart/items/{product_id}`** – Задаване на точно количество на продукт (идемпотентна операция):
  - Изисква хедър: `Authorization: Bearer <token>`
  - Тяло на заявката:
    ```json
    {
      "quantity": 3
    }
    ```
  - Валидация:
    - `quantity`: строго цяло число между 1 и 99.
    - Стойности като `0`, `<= -1`, `> 99`, плаваща запетая (`float`), `boolean`, `null`, празно тяло `{}` или допълнителни непознати полета се отхвърлят с `422 Unprocessable Entity`.
  - Поведение и отговори:
    - `200 OK`: връща пълната обновена количка (`CartResponse`).
    - `401 Unauthorized`: при липсващ или невалиден Bearer токен.
    - `404 Not Found` (`PRODUCT_NOT_FOUND`): ако продуктът не съществува или е деактивиран (`active: false`).
    - `409 Conflict` (`INSUFFICIENT_STOCK`): ако заявеното количество надвишава текущата складова наличност (`quantity > stock`). Количката остава непроменена.
    - Използва атомарен PostgreSQL upsert (`ON CONFLICT DO UPDATE`), гарантиращ липса на колизии при конкурентни заявки.

- **`DELETE /cart/items/{product_id}`** – Премахване на продукт от количката:
  - Изисква хедър: `Authorization: Bearer <token>`
  - Поведение и отговори:
    - `204 No Content`: успешно премахване (празно тяло).
    - Идемпотентност: ако продуктът не е бил в количката, отново връща `204 No Content`.
    - Безопасност: работи успешно (204) дори ако продуктът е бил деактивиран в каталога или напълно липсва.
    - `401 Unauthorized`: при липсващ или невалиден Bearer токен.

---

### 4. Поръчки (Orders)

- **`POST /orders`** – Оформяне на текущата количка в поръчка със статус `pending_payment`:
  - Изисква хедъри:
    - `Authorization: Bearer <token>`
    - `Idempotency-Key: <UUID>` (валиден UUID v4)
  - Изисква тяло: празен JSON обект `{}` (всякакви допълнителни полета се отхвърлят с `422`).
  - Поведение и отговори:
    - `201 Created`: успешно оформена поръчка с пълен детайл и списък със запазени позиции (`OrderDetailResponse`).
    - `401 Unauthorized`: при липсващ или невалиден токен.
    - `422 Unprocessable Entity`: при липсващ/невалиден UUID в `Idempotency-Key` или невалидно тяло.
    - `409 Conflict` (`CART_EMPTY`): ако потребителската количка е празна.
    - `409 Conflict` (`CART_PRODUCT_UNAVAILABLE`): ако продукт от количката е деактивиран или не съществува в каталога (връща и засегнатия `product_id`).
    - `409 Conflict` (`INSUFFICIENT_STOCK`): ако складовата наличност не достига за заявеното количество (връща и засегнатия `product_id`).
    - Идемпотентност: повторно изпращане със същия `Idempotency-Key` от същия потребител връща съществуващата поръчка с `201 Created`, без повторно намаляване на склада и без промяна в количката.

- **`GET /orders`** – Списък със собствените поръчки на текущия потребител:
  - Изисква хедър: `Authorization: Bearer <token>`
  - Query параметри:
    - `limit`: брой записи на страница (по подразбиране `20`, диапазон `1–100`).
    - `offset`: отместване от началото (по подразбиране `0`, минимум `0`).
  - Подредба: строго `created_at DESC, id DESC`.
  - Поведение и отговори:
    - `200 OK`: връща пагиниран списък (`OrderListResponse`) с обобщена информация (`OrderSummaryResponse`) без вложени позиции.
    - `401 Unauthorized`: при липсващ или невалиден токен.

- **`GET /orders/{order_id}`** – Преглед на единична собствена поръчка с нейните позиции:
  - Изисква хедър: `Authorization: Bearer <token>`
  - Поведение и отговори:
    - `200 OK`: детайлна поръчка със запазените исторически заглавия, цени и количества, подредени по `product_id` възходящо (`OrderDetailResponse`).
    - `401 Unauthorized`: при липсващ или невалиден токен.
    - `404 Not Found` (`ORDER_NOT_FOUND`): ако поръчката не съществува или принадлежи на друг потребител (строга изолация, без административен достъп до чужди поръчки в този обхват).

---

## Демонстрация чрез cURL и Swagger UI

### 1. Пълен цикъл за администратор (Registration -> Promote -> Login -> Create Product -> View Product)

#### Стъпка 1: Регистрация на нов потребител (създава се като customer)
```bash
curl -X POST http://localhost:8000/users \
  -H "Content-Type: application/json" \
  -d '{"email": "manager@example.com", "password": "SuperSecretPassphrase123!"}'
```

#### Стъпка 2: Локално повишаване до роля `admin` през CLI
```bash
# В работещия контейнер (изпълнява се от корена на хранилището):
docker compose --env-file services/commerce/.env exec api python scripts/promote_admin.py manager@example.com

# Или локално:
cd services/commerce && uv run python scripts/promote_admin.py manager@example.com
```

#### Стъпка 3: Вход в системата (Login) и извличане на JWT токен чрез JSON парсер
```bash
ADMIN_TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "manager@example.com", "password": "SuperSecretPassphrase123!"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
```

#### Стъпка 4: Създаване на нов продукт (`POST /products`) и извличане на реалното ID
```bash
CREATE_RESPONSE=$(curl -s -X POST http://localhost:8000/products \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{
    "title": "Ergonomic Mechanical Keyboard RGB",
    "price_minor": 14999,
    "currency": "EUR",
    "stock": 15,
    "active": true
  }')

# Извличаме реалното ID, генерирано от базата данни:
PRODUCT_ID=$(echo "$CREATE_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
```

#### Стъпка 5: Преглед на създадения продукт по реалното ID (`GET /products/{id}`)
```bash
curl -X GET "http://localhost:8000/products/$PRODUCT_ID"
```
*Връща `200 OK` с данните за новосъздадения продукт:*
```json
{
  "id": 2,
  "title": "Ergonomic Mechanical Keyboard RGB",
  "price_minor": 14999,
  "currency": "EUR",
  "stock": 15,
  "active": true
}
```

#### Стъпка 6: Редактиране, деактивиране и повторно активиране (`PATCH /products/{id}`)

Обновяване само на цената и наличността (останалите полета се запазват):
```bash
curl -X PATCH "http://localhost:8000/products/$PRODUCT_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"price_minor": 12999, "stock": 20}'
```

Деактивиране на продукта (веднага се скрива от публичните GET заявки):
```bash
curl -X PATCH "http://localhost:8000/products/$PRODUCT_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"active": false}'
```
*След това `GET /products/$PRODUCT_ID` връща `404 PRODUCT_NOT_FOUND`.*

Повторно активиране:
```bash
curl -X PATCH "http://localhost:8000/products/$PRODUCT_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"active": true}'
```

### 2. Използване през Swagger UI (`/docs`)

1. Отворете [http://localhost:8000/docs](http://localhost:8000/docs).
2. Изпълнете `POST /auth/login` с данни на администратор и копирайте върнатия `access_token`.
3. Натиснете бутона **Authorize** (горе вдясно с катинарчето).
4. В полето **Value** поставете копирания токен (Swagger UI автоматично добавя `Bearer ` префикса).
5. Натиснете **Authorize**, след което **Close**.
6. Вече можете да извиквате `POST /products`, `PATCH /products/{product_id}` и `GET /users/me` директно от интерфейса.

### 3. Работа с публичния каталог
```bash
# Извличане на продукти с пагинация:
curl "http://localhost:8000/products?limit=5&offset=0"

# Преглед на детайл за единичен продукт:
curl "http://localhost:8000/products/1"
```

### 4. Управление на потребителска количка (Cart Walkthrough)

```bash
# 1. Вход като клиент и извличане на токен
CUSTOMER_TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "customer@example.com", "password": "SuperSecretPassphrase123!"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# 2. Преглед на текущата количка (първоначално празна)
curl -X GET http://localhost:8000/cart \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"

# 3. Добавяне / задаване на количество от продукт #1 (например 2 бр.)
curl -X PUT http://localhost:8000/cart/items/1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -d '{"quantity": 2}'

# 4. Идемпотентно обновяване на количеството (напр. до 4 бр.)
curl -X PUT http://localhost:8000/cart/items/1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -d '{"quantity": 4}'

# 5. Опит за заявяване на количество, надвишаващо наличността (връща 409 INSUFFICIENT_STOCK)
curl -X PUT http://localhost:8000/cart/items/1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -d '{"quantity": 99}'

# 6. Изтриване на позиция от количката (връща 204 No Content)
curl -i -X DELETE http://localhost:8000/cart/items/1 \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"
```

### 5. Оформяне и управление на поръчки (Orders Walkthrough)

```bash
# 1. Добавяне на продукт в количката
curl -X PUT http://localhost:8000/cart/items/1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -d '{"quantity": 2}'

# 2. Генериране на UUID за Idempotency-Key
IDEMPOTENCY_KEY=$(python3 -c "import uuid; print(uuid.uuid4())")

# 3. Оформяне на количката в поръчка (POST /orders с Idempotency-Key и празно тяло {})
ORDER_RESPONSE=$(curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d '{}')
echo "$ORDER_RESPONSE"

ORDER_ID=$(python3 -c "import json; print(json.loads('''$ORDER_RESPONSE''')['id'])")

# 4. Повторно изпращане със СЪЩИЯ Idempotency-Key (идемпотентност -> връща 201 и същата поръчка)
curl -i -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d '{}'

# 5. Преглед на пагинирания списък с поръчки на потребителя
curl -s http://localhost:8000/orders?limit=10&offset=0 \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"

# 6. Преглед на единична поръчка с нейните запазени позиции
curl -s http://localhost:8000/orders/$ORDER_ID \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"
```

### 6. Отказ от поръчка и възстановяване на наличност (Order Cancellation Walkthrough)

```bash
# 1. Проверка на началната наличност на продукт #1
curl -s http://localhost:8000/products/1 | grep stock

# 2. Добавяне в количката и оформяне на нова поръчка (за 2 бр.)
curl -s -X PUT http://localhost:8000/cart/items/1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -d '{"quantity": 2}'

NEW_ORDER_KEY=$(python3 -c "import uuid; print(uuid.uuid4())")
NEW_ORDER=$(curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Idempotency-Key: $NEW_ORDER_KEY" \
  -d '{}')
CANCEL_ORDER_ID=$(python3 -c "import json; print(json.loads('''$NEW_ORDER''')['id'])")

# Наличността на продукт #1 е намалена с 2 (резервирана за новата поръчка):
curl -s http://localhost:8000/products/1 | grep stock

# 3. Отказ от поръчката от клиента (POST /orders/{id}/cancel) -> 200 OK (status: cancelled)
curl -i -X POST http://localhost:8000/orders/$CANCEL_ORDER_ID/cancel \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"

# Наличността в каталога е точно възстановена с +2 (вкл. за деактивирани продукти):
curl -s http://localhost:8000/products/1 | grep stock

# 4. Двоен отказ -> 200 OK (идемпотентен, без повторно връщане на stock):
curl -i -X POST http://localhost:8000/orders/$CANCEL_ORDER_ID/cancel \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"
curl -s http://localhost:8000/products/1 | grep stock
```

### 7. Плащане чрез Stripe Checkout (Sandbox / Live), отложени методи и сверка

Модулът поддържа пълна интеграция със Stripe чрез официалния Stripe Python SDK и хоствана платежна страница (Stripe Checkout) в режим `mode="payment"` с поддръжка както на мигновени плащания (карти), така и на отложени методи (напр. SEPA Direct Debit, банкови преводи).

#### 1. Таблица на състоянията и преходите

| Начален статус на поръчката | Статус на опита (`attempt`) | Събитие / Действие | Краен статус на поръчката | Краен статус на опита | Складова наличност (`stock`) | Забележка |
|---|---|---|---|---|---|---|
| `pending_payment` | — / `expired` / `failed` | `POST /checkout-session` | `pending_payment` | `open` (след `initiated`) | Запазва се резерва | Нов опит се позволява само ако няма активна сесия и срокът не е изтекъл. |
| `pending_payment` | `open` | `POST /checkout-session` | `pending_payment` | `open` | Запазва се резерва | Идемпотентно връща съществуващия `checkout_url`. |
| `pending_payment` | `open` | Webhook `checkout.session.completed` (`paid`) | **`paid`** | **`completed`** | Запазва се резерва | Мигновено потвърдено плащане (карта). Наличност НЕ се намалява втори път. |
| `pending_payment` | `open` | Webhook `checkout.session.completed` (`unpaid`) | **`payment_processing`** | **`processing`** | Запазва се резерва | Клиентът завършва Checkout с отложен метод. Отказът и втори линк се блокират (409). |
| `payment_processing` | `processing` | Webhook `checkout.session.async_payment_succeeded` | **`paid`** | **`completed`** | Запазва се резерва | Парите са потвърдени асинхронно. Дори `completed` да е дошъл със закъснение. |
| `payment_processing` | `processing` | Webhook `checkout.session.async_payment_failed` (срокът не е изтекъл) | **`pending_payment`** | **`failed`** | Запазва се резерва | Клиентът може да опита отново да плати с нов линк преди изтичане на срока. |
| `payment_processing` | `processing` | Webhook `checkout.session.async_payment_failed` (срокът е изтекъл) | **`expired`** | **`failed`** | **Възстановява се (+qty)** | Резервацията е изтекла и плащането е отказано -> стоката се връща в склада. |
| `pending_payment` | `open` | Webhook `checkout.session.expired` (срокът е изтекъл) | **`expired`** | **`expired`** | **Възстановява се (+qty)** | Stripe сесията е изтекла и срокът на поръчката е минал. |
| `pending_payment` | `open` | `POST /orders/{id}/cancel` | **`cancelled`** | **`expired`** | **Възстановява се (+qty)** | Сесията се прекратява в Stripe, поръчката става `cancelled`, стоката се връща веднъж. |
| `payment_processing` | `processing` | `POST /orders/{id}/cancel` | `payment_processing` | `processing` | Запазва се резерва | **409 ORDER_STATUS_CONFLICT**: Забранен отказ, докато банката обработва превода. |
| `cancelled` / `expired` | `open` / `processing` | Закъсняло плащане (`paid`) | `cancelled` / `expired` | `completed` | Запазва се (не се пипа) | **Разхождение (`discrepancy`)**: Плащането се записва за ръчен одит, stock не се променя. |

#### 2. Архитектура на транзакциите и блокировките

- **Разделена собственост**: Payments заключва в своята БД (`payments`) реда чрез PostgreSQL advisory lock (`pg_advisory_xact_lock(order_id)`) → `PaymentAttempt` / `StripeCheckoutSession`, а Commerce заключва в своята БД (`commerce`) само `Order` (`FOR UPDATE`) → `Product` (възходящо по `id`).
- **Без отворени DB транзакции по време на мрежови заявки**: И двата сервиса комитват DB транзакцията преди HTTP обръщения (Commerce → Payments и Payments → Stripe API).
- **Идемпотентност**: Payments използва `stripe_processed_events`, стабилен `attempt_key` и `payment_outbox_events`; Commerce използва `order_inbox_events`, `order_outbox_events` и `order_payment_discrepancies`.

#### 3. Периодична сверка и управление на резервите

Всяка поръчка има срок на валидност на резерва (`expires_at`, конфигуриран чрез `ORDER_RESERVE_TIMEOUT_MINUTES`, по подразбиране 30 минути).
- **Сверка на просрочени резерви в Commerce (`services/commerce/scripts/reconcile_orders.py`)**: изтичането на срока е повод Commerce да попита Payments (`/internal/payments/orders/{id}/cancel` с `reason="reserve_expired"`). Резервът се освобождава единствено ако Payments потвърди липса на активно/обработващо се плащане.
- **Сверка с провайдера в Payments (`services/payments/scripts/reconcile_payments.py`)**: проверява незавършени опити директно в Stripe API и при пропуснат webhook публикува `payment.succeeded` / `payment.expired` през `payment_outbox_events`.

```bash
cd services/commerce && uv run python scripts/reconcile_orders.py
cd services/payments && uv run python scripts/reconcile_payments.py
```

#### 4. Stripe CLI команди за 4-те webhook събития

Стартирайте локално препредаване на всички 4 събития директно към **Payments** (`localhost:8001`):
```bash
stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired --forward-to http://localhost:8001/webhooks/stripe
```

Симулиране на всяко от събитията чрез Stripe CLI (за бърз диспечерски тест):
1. **Успешно приключване на Checkout**:
   ```bash
   stripe trigger checkout.session.completed
   ```
2. **Асинхронно потвърдено плащане (отложен метод)**:
   ```bash
   stripe trigger checkout.session.async_payment_succeeded
   ```
3. **Неуспешно асинхронно плащане**:
   ```bash
   stripe trigger checkout.session.async_payment_failed
   ```
4. **Изтичане на платежна сесия**:
   ```bash
   stripe trigger checkout.session.expired
   ```

#### 5. Ръчна демонстрация в Swagger на отложено плащане (SEPA / Deferred)

1. **Конфигурация на средата (`services/payments/.env`)**:
   ```env
   STRIPE_MODE=test
   STRIPE_SECRET_KEY=sk_test_...
   STRIPE_WEBHOOK_SECRET=whsec_...
   PAYMENTS_INTERNAL_AUTH_TOKEN=...
   ```
2. **Стартирайте Stripe CLI слушалтеля към Payments (`8001`)**:
   ```bash
   stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired --forward-to http://localhost:8001/webhooks/stripe
   ```
3. **Оформете поръчка**:
   - Влезте в `http://localhost:8000/docs`, автентикирайте се с клиентски JWT токен.
   - Добавете артикул в количката (`PUT /cart/items/{product_id}`).
   - Създайте поръчка чрез `POST /orders` с хедър `Idempotency-Key: <UUID>`. Запомнете върнатия `id`.
4. **Генерирайте платежен линк**:
   - Извикайте `POST /orders/{order_id}/checkout-session`.
   - Отворете върнатия `checkout_url` в браузъра си.
5. **Изберете отложен платежен метод в Stripe Checkout**:
   - Изберете **SEPA Direct Debit** (или друг наличен отложен метод в тестовия ви Stripe акаунт).
   - Въведете тестови IBAN: `DE89 3704 0044 0532 0130 00` и произволно име.
   - Потвърдете плащането. Stripe ще ви пренасочи към информационния success URL.
6. **Проверете междинния статус в бекенда**:
   - Webhook-ът получава `checkout.session.completed` с `payment_status="unpaid"`.
   - Проверете `GET /orders/{order_id}` в Swagger: статусът е **`payment_processing`**.
   - Опитайте `POST /orders/{order_id}/cancel`: бекендът връща **`409 ORDER_STATUS_CONFLICT`** (отказът е блокиран).
   - Опитайте `POST /orders/{order_id}/checkout-session`: бекендът връща **`409 ORDER_STATUS_CONFLICT`** (втори линк е блокиран).
   - Наличността в каталога остава резервирана.
7. **Симулирайте финално асинхронно потвърждение**:
   - През Stripe Dashboard (Payments → Incomplete / Processing) или Stripe CLI маркирайте плащането като успешно:
     ```bash
     stripe trigger checkout.session.async_payment_succeeded
     ```
   - След обработка на събитието проверете `GET /orders/{order_id}`: статусът става **`paid`**.

#### 6. Инструкция за преминаване към Live среда

За работа с реални разплащания се изисква:
1. **Променлива на средата**:
   ```env
   STRIPE_MODE=live
   ```
2. **Валидирани live ключове**:
   - `STRIPE_SECRET_KEY`: задължително започва с префикс `sk_live_`. Приложението ще отхвърли стартиране или зареждане на настройки с тестов ключ в live режим.
   - `STRIPE_WEBHOOK_SECRET`: реален webhook secret от Stripe Production Dashboard (`whsec_...`).
   - `STRIPE_SUCCESS_URL` и `STRIPE_CANCEL_URL`: задължително започват с **`https://`** (криптографски защитени публични адреси).
3. **Валидация на входящи събития**:
   - Бекендът сравнява `event.livemode == True`. Ако събитие от sandbox попадне в live бекенд (или обратно), то се отхвърля със статус `ignored` и причина `mode_mismatch`.
4. **База данни**:
   - Всички създавани опити се маркират автоматично с `mode = 'live'` в колоната `mode` на `stripe_checkout_sessions`.
5. **Миграции**:
   - Уверете се, че базата данни е обновена до последната миграция: `uv run alembic upgrade head`.
Стартирайте локално препредаване на уебхуци чрез Stripe CLI към Payments:
```bash
stripe listen --events checkout.session.completed --forward-to http://localhost:8001/webhooks/stripe
```
Копирайте изписания webhook secret (напр. `whsec_...`) и го въведете в `services/payments/.env`:
```env
STRIPE_WEBHOOK_SECRET=whsec_...
```

##### 2. Автентикация в Swagger UI
1. Отворете `http://localhost:8000/docs`.
2. Регистрирайте нов потребител през `POST /users` или влезте през `POST /auth/login`.
3. Копирайте върнатия `access_token` и кликнете на зеления бутон **Authorize** горе вдясно (въведете `Bearer <вашият_токен>`).

##### 3. Сценарий A: Успешно плащане (Success Card)
1. Добавете продукт в количката (`PUT /cart/items/{product_id}`).
2. Оформете поръчка чрез `POST /orders` (с подаден `Idempotency-Key` UUID хедър и тяло `{}`). Копирайте `id` на поръчката.
3. Извикайте `POST /orders/{order_id}/checkout-session`. В отговора ще получите `checkout_url` и `session_id`.
4. Отворете `checkout_url` в браузъра си. Ще видите хостваната от Stripe страница с вашите продукти и обща сума в EUR.
5. Въведете тестова карта за успешен резултат:
   - **Номер на карта**: `4242 4242 4242 4242`
   - **Валидност**: всяка бъдеща дата (напр. `12/34`)
   - **CVC**: три произволни цифри (напр. `123`)
   - **Име и държава**: произволни
6. Натиснете **Pay**. Stripe обработва транзакцията и ви пренасочва към success URL.
7. В терминала със `stripe listen` ще видите входящото събитие към Payments (`8001`):
   ```text
   --> checkout.session.completed [evt_...]
   <-- [200] POST http://localhost:8001/webhooks/stripe
   ```
8. `payments-outbox` публикува `payment.succeeded` към RabbitMQ, `commerce-payment-consumer` отбелязва поръчката като `paid` и записва `order.paid` в `order_outbox_events`, а `commerce-outbox` + `notifications` доставят имейла в Mailpit (`http://localhost:8025`) и SMS (в `test` режим).

##### 4. Сценарий B: Отхвърлена карта (Declined Card)
1. Оформете нова поръчка (`POST /orders`) и генерирайте платежен линк (`POST /orders/{order_id}/checkout-session`).
2. Отворете новия `checkout_url` и въведете тестова карта за отказ:
   - **Номер на карта**: `4000 0000 0000 0002` (Your card was declined)
   - **Валидност**: `12/34`, **CVC**: `123`
3. Натиснете **Pay**. Stripe показва съобщение за отхвърлена карта.
4. Проверете поръчката в Swagger (`GET /orders/{order_id}`):
   - Поръчката остава в статус **`pending_payment`**.
   - Стоката остава резервирана, а клиентът може да повтори опита за плащане.

##### 5. Сценарий C: Отказ от поръчка при отворена Stripe сесия
1. Оформете поръчка и генерирайте Stripe Checkout линк.
2. Преди да платите в Stripe, извикайте в Swagger `POST /orders/{order_id}/cancel`.
3. Отговорът връща поръчката със статус **`cancelled`**, а наличностите в `products` са точно възстановени.
4. Върнете се на отворения в браузъра `checkout_url` и опитайте да платите: Stripe съобщава, че сесията е изтекла (`expired`) и не позволява плащане по анулираната поръчка.

---

## Архитектурни бележки и технически ограничения

- **Администраторски контрол**: `POST /products` и `PATCH /products/{product_id}` са достъпни единствено за потребители с роля `admin`. Потребители с роля `customer` получават `403 FORBIDDEN` (`detail.code = "FORBIDDEN"`). Операциите `DELETE` и `PUT` за продукти съзнателно **не са** част от тази учебна стъпка.
- **Синхронизиране на ID Sequence**: Чрез миграцията `4af78c666cf2` PostgreSQL sequence-ът се синхронизира автоматично с максималното съществуващо ID. Новите продукти получават автоматични ID-та без ръчна намеса и без необходимост от `setval` при всяка HTTP заявка.
- **Транзакционен протокол за поръчки и количка**:
  - Всяка мутация (checkout, добавяне или премахване от количка) придобива ред-блокировка върху потребителя с `FOR NO KEY UPDATE` в PostgreSQL преди каквито и да е други четения или промени.
  - При оформяне на поръчка продуктите се заключват с `FOR UPDATE` в строго възходящ ред по ID (`ORDER BY id ASC`), което напълно елиминира риска от deadlocks между конкурентни процеси.
  - Намаляването на складовата наличност при статус `pending_payment` представлява резервация на стоката.
- **Жизнен цикъл на поръчките и симулирани плащания**:
  - Разграничение на концепциите: резултат от опит за плащане е `succeeded` или `declined`; статус на поръчката е `pending_payment`, `paid` или `cancelled`.
  - Плащането и отказът заключват един и същи ред на поръчката с `SELECT ... FOR UPDATE` в PostgreSQL преди решение за промяна.
  - При отказ засегнатите продукти се заключват във възходящ ред по ID, а запазените количества се връщат точно в `stock` (включително за продукт, деактивиран след checkout). Количката не се възстановява.
  - На ниво PostgreSQL уникалността се гарантира с `(order_id, idempotency_key)` и частичен уникален индекс за най-много един успешен опит на поръчка (`WHERE status = 'succeeded'`).
- **Transactional Outbox и независим Notifications сервис**:
  - При преход към `paid` (`order.paid`) и при изпращане на пратка (`shipment.shipped`) събитието със стабилен `event_id` и `event_version=1` се записва в `order_outbox_events` в същата PostgreSQL транзакция.
  - Webhook и shipment заявките никога не чакат RabbitMQ, SMTP или SMS провайдъра.
  - Отделният `commerce-outbox` процес (`services/commerce/scripts/process_outbox.py`) публикува в RabbitMQ с `delivery_mode=2`, `mandatory=True` и publisher confirms и отбелязва `'published'` едва след потвърждение от брокера и проверен маршрут.
  - Независимият `services/notifications` сервис записва събитието и каналните задачи (`email`, `sms`) в собствена база (`notifications`) преди да изпрати `basic_ack` към RabbitMQ, а доставката се извършва от разделни работни цикли с лезинг и индивидуални опити/закъснения на канал.
- **Кратковременни токени**: Access токенът е валиден 15 минути. В тази стъпка нарочно **не са** включени refresh tokens или списък за оттегляне (token revocation / denylist), за да се запази архитектурата проста и без излишен овърхед.
- **Stateless проверка с актуалност от БД**: Токенът се проверява криптографски с HMAC-SHA256 (`HS256`). Потребителският запис се зарежда директно от базата данни при всяка защитена заявка, което гарантира, че незабавно се отчита всяка промяна на ролята дори със съществуващ токен.
- **Изисквания за продукционна среда**:
  - **HTTPS/TLS**: Задължително е целият трафик да минава през криптиран HTTPS канал за предпазване на bearer токените и паролите при пренос по мрежата.
  - **Rate Limiting**: За крайния път `/auth/login` е задължително налагането на ограничение на броя опити (rate limiting срещу brute-force и credential stuffing атаки) преди публично внедряване.
