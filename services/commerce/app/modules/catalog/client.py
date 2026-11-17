"""Вътрешен HTTP клиент на Commerce за комуникация с микросервиса Catalog."""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


class CatalogServiceUnavailableError(Exception):
    """Грешка при комуникация или таймаут към Catalog сервиса."""


class CatalogServiceAuthError(Exception):
    """Грешка при автентикация на Commerce към вътрешния API на Catalog."""


class CatalogInsufficientStockError(Exception):
    """Сигнализира за недостиг на наличност за продукт."""

    def __init__(self, product_id: int, message: str = "Insufficient stock") -> None:
        super().__init__(message)
        self.product_id = product_id


class CatalogProductUnavailableError(Exception):
    """Сигнализира, че продуктът е деактивиран или продавачът е неактивен."""

    def __init__(self, product_id: int, message: str = "Product unavailable") -> None:
        super().__init__(message)
        self.product_id = product_id


@dataclass(frozen=True)
class RemoteProductInfo:
    """Модел за данни на продукт от Catalog сервиса."""

    id: int
    seller_id: int
    category_id: int | None
    title: str
    price_minor: int
    currency: str
    stock: int
    active: bool
    seller_active: bool = True


class CatalogClient:
    """Синхронен httpx клиент към микросервиса Catalog."""

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
        self.transport = transport

    def _internal_headers(self) -> dict[str, str]:
        headers = {}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    def get_product(self, product_id: int) -> RemoteProductInfo | None:
        """Извлича продукт от Catalog."""
        url = f"{self.base_url}/internal/products/{product_id}"
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                resp = client.get(url, headers=self._internal_headers())
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise CatalogServiceUnavailableError(
                f"Catalog service unavailable: {type(exc).__name__}"
            ) from exc

        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise CatalogServiceAuthError(
                f"Unauthorized internal request to Catalog service: {resp.status_code}"
            )
        if resp.status_code != 200:
            raise CatalogServiceUnavailableError(
                f"Catalog service returned unexpected status: {resp.status_code}"
            )

        data = resp.json()
        return RemoteProductInfo(
            id=data["id"],
            seller_id=data["seller_id"],
            category_id=data.get("category_id"),
            title=data["title"],
            price_minor=data["price_minor"],
            currency=data["currency"],
            stock=data["stock"],
            active=data["active"],
            seller_active=data.get("seller_active", True),
        )

    def get_products_batch(self, product_ids: list[int]) -> list[RemoteProductInfo]:
        """Пакетно извличане на продукти."""
        if not product_ids:
            return []
        url = f"{self.base_url}/internal/products/batch"
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                resp = client.post(
                    url,
                    json={"product_ids": product_ids},
                    headers=self._internal_headers(),
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise CatalogServiceUnavailableError(
                f"Catalog service unavailable: {type(exc).__name__}"
            ) from exc

        if resp.status_code in (401, 403):
            raise CatalogServiceAuthError(
                f"Unauthorized internal request to Catalog service: {resp.status_code}"
            )
        if resp.status_code != 200:
            raise CatalogServiceUnavailableError(
                f"Catalog service returned unexpected status: {resp.status_code}"
            )

        return [
            RemoteProductInfo(
                id=item["id"],
                seller_id=item["seller_id"],
                category_id=item.get("category_id"),
                title=item["title"],
                price_minor=item["price_minor"],
                currency=item["currency"],
                stock=item["stock"],
                active=item["active"],
                seller_active=item.get("seller_active", True),
            )
            for item in resp.json()
        ]

    def reserve_stock(self, order_id: int, items: list[dict[str, int]]) -> None:
        """Атомарно резервиране на наличност по поръчка."""
        url = f"{self.base_url}/internal/stock/reserve"
        payload = {"order_id": order_id, "items": items}
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                resp = client.post(url, json=payload, headers=self._internal_headers())
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise CatalogServiceUnavailableError(
                f"Catalog service unavailable: {type(exc).__name__}"
            ) from exc

        if resp.status_code == 200:
            return

        if resp.status_code == 409:
            try:
                err = resp.json().get("detail", {})
                code = err.get("code")
                pid = err.get("product_id", 0)
                if code == "INSUFFICIENT_STOCK":
                    raise CatalogInsufficientStockError(
                        pid, err.get("message", "Insufficient stock")
                    )
                if code == "PRODUCT_UNAVAILABLE":
                    raise CatalogProductUnavailableError(
                        pid, err.get("message", "Product unavailable")
                    )
            except (ValueError, KeyError):
                pass
            raise CatalogProductUnavailableError(0, "Stock reservation conflict")

        if resp.status_code in (401, 403):
            raise CatalogServiceAuthError(
                f"Unauthorized internal request to Catalog service: {resp.status_code}"
            )
        raise CatalogServiceUnavailableError(
            f"Catalog service returned unexpected status: {resp.status_code}"
        )

    def release_stock(self, order_id: int) -> None:
        """Идемпотентно освобождаване на наличност по поръчка."""
        url = f"{self.base_url}/internal/stock/release"
        payload = {"order_id": order_id}
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                resp = client.post(url, json=payload, headers=self._internal_headers())
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise CatalogServiceUnavailableError(
                f"Catalog service unavailable: {type(exc).__name__}"
            ) from exc

        if resp.status_code == 200:
            return

        if resp.status_code in (401, 403):
            raise CatalogServiceAuthError(
                f"Unauthorized internal request to Catalog service: {resp.status_code}"
            )
        raise CatalogServiceUnavailableError(
            f"Catalog service returned unexpected status: {resp.status_code}"
        )

    def forward_request(
        self,
        method: str,
        path: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Проксира публична заявка към Catalog сервиса."""
        url = f"{self.base_url}{path}"
        filtered_headers = {}
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("host", "content-length"):
                    filtered_headers[k] = v

        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                return client.request(
                    method=method,
                    url=url,
                    content=content,
                    headers=filtered_headers,
                    params=params,
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise CatalogServiceUnavailableError(
                f"Catalog service unavailable: {type(exc).__name__}"
            ) from exc
