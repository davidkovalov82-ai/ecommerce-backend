FROM python:3.13-slim

WORKDIR /app

# Инсталиране на uv от официалния образ
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

# Инсталиране на зависимостите (кеширане на слой)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Копиране на сорс кода и миграциите
COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

CMD ["sh", "-c", "alembic upgrade head && python -m app.main"]

