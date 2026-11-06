# E-Commerce Backend

Модерен, модулен бекенд за електронна търговия, изграден с FastAPI, PostgreSQL, SQLAlchemy 2.0 и Alembic. Проектът използва `uv` като бърз мениджър на пакети и поддържа контейнеризация с Docker и Docker Compose.

---

## Архитектура на проекта

Системата е структурирана като модулен монолит (modular monolith), разделен на независими функционални модули в `services/commerce/app/modules/`:

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

### 1. Подготовка на средата и генериране на JWT_SECRET

Преди първоначалното стартиране на контейнерите е необходимо да подготвите `.env` файла (ако все още не съществува) и да генерирате сигурен таен ключ за JWT токените.

Копирайте примерната конфигурация **без да презаписвате** съществуващ `.env` файл:
```bash
test -f services/commerce/.env || cp services/commerce/.env.example services/commerce/.env
```

Генерирайте сигурен таен ключ (поне 32 байта):
```bash
# Пример чрез Python secrets:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# или чрез openssl:
openssl rand -hex 32
```

Отворете `services/commerce/.env` и попълнете генерирания ключ срещу `JWT_SECRET`:
```env
JWT_SECRET=вашият_генериран_ключ_поне_32_байта
```
*(Забележка: няма работеща стойност по подразбиране – задаването на непразен ключ от поне 32 байта е задължително).*

### 2. Стартиране на контейнерите (от корена на хранилището)

```bash
docker compose --env-file services/commerce/.env up --build
```

Compose автоматично:
1. Стартира PostgreSQL контейнер и изчаква готовността му (healthcheck).
2. Построява контейнера на Commerce API чрез `Dockerfile`.
3. Прилага автоматично последните Alembic миграции (`alembic upgrade head`).
4. Стартира API сървъра на адрес `http://localhost:8000`.

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

### Интерактивна документация
Отворете браузъра си на:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **OpenAPI JSON**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

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

### 2. Стартиране само на базата данни (от корена на хранилището)

```bash
docker compose --env-file services/commerce/.env up -d db
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

---

## Тестване и проверка на кода

Проектът разполага с пълен набор от автоматизирани тестове, използващи отделна изолирана тестова база (`commerce_test`):

### Изпълнение на всички тестове

```bash
cd services/commerce
uv run pytest
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

### 6. Симулирано плащане и отказ от поръчка (Payments & Cancellation Walkthrough)

#### Демонстрация 1: Отказано плащане → успешен повторен опит

```bash
# 1. Първи опит с отказан фиктивен метод (test_declined) -> 201 Created (status: declined)
PAY_KEY_1=$(python3 -c "import uuid; print(uuid.uuid4())")
curl -i -X POST http://localhost:8000/orders/$ORDER_ID/payments \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Idempotency-Key: $PAY_KEY_1" \
  -d '{"payment_method": "test_declined"}'

# Поръчката остава в pending_payment и резервацията на stock е запазена:
curl -s http://localhost:8000/orders/$ORDER_ID \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" | grep status

# 2. Повторение със СЪЩИЯ ключ и същия метод -> 200 OK (връща същия declined опит без симулация)
curl -i -X POST http://localhost:8000/orders/$ORDER_ID/payments \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Idempotency-Key: $PAY_KEY_1" \
  -d '{"payment_method": "test_declined"}'

# 3. Успешен повторен опит с НОВ ключ (test_success) -> 201 Created (status: succeeded)
PAY_KEY_2=$(python3 -c "import uuid; print(uuid.uuid4())")
curl -i -X POST http://localhost:8000/orders/$ORDER_ID/payments \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Idempotency-Key: $PAY_KEY_2" \
  -d '{"payment_method": "test_success"}'

# Поръчката преминава в статус 'paid', наличността в склада НЕ се намалява повторно:
curl -s http://localhost:8000/orders/$ORDER_ID \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" | grep status
```

#### Демонстрация 2: Отделна поръчка → отказ от клиента и възстановена наличност

```bash
# 1. Проверка на началната наличност на продукт #1
curl -s http://localhost:8000/products/1 | grep stock

# 2. Добавяне в количката и оформяне на нова отделна поръчка (за 2 бр.)
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
- **Кратковременни токени**: Access токенът е валиден 15 минути. В тази стъпка нарочно **не са** включени refresh tokens или списък за оттегляне (token revocation / denylist), за да се запази архитектурата проста и без излишен овърхед.
- **Stateless проверка с актуалност от БД**: Токенът се проверява криптографски с HMAC-SHA256 (`HS256`). Потребителският запис се зарежда директно от базата данни при всяка защитена заявка, което гарантира, че незабавно се отчита всяка промяна на ролята дори със съществуващ токен.
- **Изисквания за продукционна среда**:
  - **HTTPS/TLS**: Задължително е целият трафик да минава през криптиран HTTPS канал за предпазване на bearer токените и паролите при пренос по мрежата.
  - **Rate Limiting**: За крайния път `/auth/login` е задължително налагането на ограничение на броя опити (rate limiting срещу brute-force и credential stuffing атаки) преди публично внедряване.
