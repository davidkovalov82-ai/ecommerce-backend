"""Вътрешен HTTP клиент на Commerce за комуникация с микросервиса Cart."""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


class CartServiceUnavailableError(Exception):
    """Грешка при комуникация или таймаут към Cart сервиса."""


class CartServiceAuthError(Exception):
    """Грешка при автентикация към вътрешния API на Cart."""


@dataclass(frozen=True)
class RemoteCartItem:
    """Модел за позиция в количката, върната от Cart сервиса."""

    product_id: int
    title: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int
    available: bool


@dataclass(frozen=True)
class RemoteCartInfo:
    """Модел за цялостната количка, върната от Cart сервиса."""

    items: list[RemoteCartItem]
    total_minor: int
    currency: str = "EUR"


class CartClient:
    """Синхронен httpx клиент към микросервиса Cart."""

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.cart_base_url).rstrip("/")
        self.internal_token = (
            internal_token
            if internal_token is not None
            else settings.cart_internal_auth_token
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.cart_timeout_seconds
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _internal_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    def forward_request(
        self,
        method: str,
        path: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Препраща публична заявка към микросервиса Cart."""
        req_headers = dict(headers or {})
        req_headers.pop("host", None)
        req_headers.pop("content-length", None)

        try:
            return self._client.request(
                method=method,
                url=path,
                content=content,
                headers=req_headers,
                params=params,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CartServiceUnavailableError(
                f"Cart service unavailable: {exc}"
            ) from exc

    def get_cart(self, user_id: int) -> RemoteCartInfo:
        """Извлича моментна снимка на количката на потребителя през вътрешния API."""
        url = f"/internal/cart/{user_id}"
        try:
            resp = self._client.get(url, headers=self._internal_headers())
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CartServiceUnavailableError(
                f"Cart service unavailable: {exc}"
            ) from exc

        if resp.status_code == 200:
            data = resp.json()
            items = [
                RemoteCartItem(
                    product_id=it["product_id"],
                    title=it.get("title", ""),
                    quantity=it["quantity"],
                    unit_price_minor=it.get("unit_price_minor", 0),
                    subtotal_minor=it.get("subtotal_minor", 0),
                    available=it.get("available", False),
                )
                for it in data.get("items", [])
            ]
            return RemoteCartInfo(
                items=items,
                total_minor=data.get("total_minor", 0),
                currency=data.get("currency", "EUR"),
            )

        if resp.status_code == 401:
            raise CartServiceAuthError("Failed internal authentication to Cart service")

        raise CartServiceUnavailableError(
            f"Cart service returned status {resp.status_code}: {resp.text}"
        )

    def clear_purchased(self, user_id: int, product_ids: list[int]) -> dict[str, Any]:
        """Изчиства само закупените позиции от количката след успешна поръчка."""
        url = f"/internal/cart/{user_id}/clear-purchased"
        try:
            resp = self._client.post(
                url,
                headers=self._internal_headers(),
                json={"product_ids": product_ids},
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CartServiceUnavailableError(
                f"Cart service unavailable: {exc}"
            ) from exc

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 401:
            raise CartServiceAuthError("Failed internal authentication to Cart service")

        raise CartServiceUnavailableError(
            f"Cart service returned status {resp.status_code}: {resp.text}"
        )
