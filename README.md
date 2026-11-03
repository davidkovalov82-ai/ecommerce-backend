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

---

## Изисквания

- **Docker** и **Docker Compose** (препоръчително за най-лесно стартиране)
- *ИЛИ* локално:
  - **Python 3.13+**
  - **uv** (пакетен мениджър: https://docs.astral.sh/uv/)
  - **PostgreSQL 17**

---

## Бърз старт с Docker Compose

Най-лесният начин да стартирате цялото приложение (PostgreSQL + Commerce API) е чрез Docker Compose:

```bash
# Клонирайте хранилището и стартирайте контейнерите
docker compose up --build
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
   # или ако сте в services/commerce:
   docker compose down
   ```
2. Стартирайте приложението от корена:
   ```bash
   docker compose up -d
   ```
   *Забележка:* В `compose.yaml` е конфигуриран проект `name: commerce`, което гарантира, че Docker продължава да използва същия именуван volume (`commerce_postgres_data`) без загуба на данни и без конфликти в имената на контейнерите.

### Зареждане на демонстрационни данни (Seeding)
За да заредите 100 примерни продукта в работещия контейнер:

```bash
docker compose exec api python scripts/seed_products.py
```
*Забележка: Скриптът е идемпотентен – повторно изпълнение няма да дублира продуктите.*

### Интерактивна документация
Отворете браузъра си на:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **OpenAPI JSON**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## Локално разработване (без Docker за API)

Ако разработвате локално с Python и `uv`:

### 1. Стартиране само на базата данни

```bash
docker compose up -d db
```

### 2. Конфигуриране на средата

Копирайте примерния файл с настройки:
```bash
cp services/commerce/.env.example services/commerce/.env
```

Генерирайте сигурен таен ключ (поне 32 байта) за подписване на JWT токените:
```bash
# Пример чрез Python secrets:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# или чрез openssl:
openssl rand -hex 32
```

Попълнете стойността в `services/commerce/.env`:
```env
DB_HOST=localhost
DB_PORT=5432
DB_USER=commerce
DB_PASSWORD=commerce
DB_NAME=commerce

# Задължително: поне 32 байта таен ключ за JWT (няма работеща стойност по подразбиране в прод)
JWT_SECRET=your_generated_random_secret_at_least_32_bytes_long
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

---

## Демонстрация чрез cURL и Swagger UI

### 1. Пълен цикъл за автентикация (Registration -> Login -> Profile)

#### Стъпка 1: Регистрация на нов потребител
```bash
curl -X POST http://localhost:8000/users \
  -H "Content-Type: application/json" \
  -d '{"email": "alex@example.com", "password": "SuperSecretPassphrase123!"}'
```

#### Стъпка 2: Вход в системата (Login) и получаване на токен
```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "alex@example.com", "password": "SuperSecretPassphrase123!"}'
```
*Примерен отговор:*
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzM...",
  "token_type": "bearer"
}
```

#### Стъпка 3: Достъп до защитения крайпът `GET /users/me`
Запазете токена в променлива или го подайте директно:
```bash
TOKEN="<вашият_access_token>"

curl -X GET http://localhost:8000/users/me \
  -H "Authorization: Bearer $TOKEN"
```
*Връща:*
```json
{
  "id": 1,
  "email": "alex@example.com",
  "role": "customer"
}
```

### 2. Използване през Swagger UI (`/docs`)

1. Отворете [http://localhost:8000/docs](http://localhost:8000/docs).
2. Изпълнете `POST /auth/login` с вашите данни и копирайте върнатия `access_token`.
3. Натиснете бутона **Authorize** (горе вдясно с катинарчето).
4. В полето **Value** поставете копирания токен (Swagger UI автоматично управлява `Bearer` префикса чрез конфигурираната `HTTPBearer` схема).
5. Натиснете **Authorize**, след което **Close**.
6. Вече можете да извиквате `GET /users/me` директно от интерфейса.

### 3. Работа с каталога
```bash
# Извличане на продукти с пагинация:
curl "http://localhost:8000/products?limit=5&offset=0"

# Преглед на детайл за единичен продукт:
curl "http://localhost:8000/products/1"
```

---

## Архитектурни бележки и технически ограничения

- **Кратковременни токени**: Access токенът е валиден 15 минути. В тази стъпка нарочно **не са** включени refresh tokens или списък за оттегляне (token revocation / denylist), за да се запази архитектурата проста и без излишен овърхед.
- **Stateless проверка**: Токенът се проверява криптографски с HMAC-SHA256 (`HS256`). Потребителският запис се зарежда директно от базата данни при всяка защитена заявка, гарантирайки актуалност на статуса и ролята.
- **Изисквания за продукционна среда**:
  - **HTTPS/TLS**: Задължително е целият трафик да минава през криптиран HTTPS канал за предпазване на bearer токените и паролите при пренос по мрежата.
  - **Rate Limiting**: За крайния път `/auth/login` е задължително налагането на ограничение на броя опити (rate limiting срещу brute-force и credential stuffing атаки) преди публично внедряване.
