"""Вътрешен HTTP клиент на Cart за комуникация с Catalog."""

import logging

import httpx

from app.config import settings
from app.schemas import ProductInternalDetails

logger = logging.getLogger(__name__)


class CatalogServiceUnavailableError(Exception):
    """Грешка при комуникация или таймаут към Catalog сервиса."""


class CatalogClient:
    """Синхронен httpx клиент към вътрешния API на Catalog."""

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.catalog_base_url).rstrip("/")
        self.internal_token = (
            internal_token
            if internal_token is not None
            else settings.catalog_internal_auth_token
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.catalog_timeout_seconds
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        """Затваря ресурсите на httpx клиента."""
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    def get_products_batch(
        self, product_ids: list[int]
    ) -> list[ProductInternalDetails]:
        """Пакетно извличане на актуални цени, наличност и статус на продукти от Catalog."""
        if not product_ids:
            return []

        url = f"{self.base_url}/internal/products/batch"
        try:
            resp = self._client.post(
                url,
                json={"product_ids": product_ids},
                headers=self._headers(),
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.error("Failed to connect to Catalog service: %s", exc)
            raise CatalogServiceUnavailableError(
                "Catalog service is unavailable"
            ) from exc

        if resp.status_code == 200:
            data = resp.json()
            return [ProductInternalDetails.model_validate(item) for item in data]

        logger.error(
            "Catalog batch returned unexpected status %s: %s",
            resp.status_code,
            resp.text,
        )
        raise CatalogServiceUnavailableError(
            f"Catalog service returned status {resp.status_code}"
        )

    def get_product(self, product_id: int) -> ProductInternalDetails | None:
        """Извлича детайли за единичен продукт."""
        products = self.get_products_batch([product_id])
        return products[0] if products else None
