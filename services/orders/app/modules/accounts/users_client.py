"""Вътрешен HTTP клиент на Commerce за комуникация с микросервиса Users."""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


class UsersServiceUnavailableError(Exception):
    """Грешка при комуникация или таймаут към Users сервиса."""


class UsersServiceAuthError(Exception):
    """Грешка при автентикация на Commerce към вътрешния API на Users."""


@dataclass(frozen=True)
class RemoteUserInfo:
    """Модел за данни на потребител, получени от Users сервиса."""

    id: int
    email: str
    role: str
    status: str
    token_version: int
    display_name: str | None = None


class UsersClient:
    """Синхронен httpx клиент към микросервиса Users."""

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.users_base_url).rstrip("/")
        self.internal_token = (
            internal_token
            if internal_token is not None
            else settings.users_internal_auth_token
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.users_timeout_seconds
        )
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    def get_user_status(self, user_id: int) -> RemoteUserInfo | None:
        """Извлича актуалния статус, роля и token_version на потребител от Users."""
        url = f"{self.base_url}/internal/users/{user_id}"
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                resp = client.get(url, headers=self._headers())
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise UsersServiceUnavailableError(
                f"Users service unavailable: {type(exc).__name__}"
            ) from exc

        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise UsersServiceAuthError(
                f"Unauthorized internal request to Users service: {resp.status_code}"
            )
        if resp.status_code != 200:
            raise UsersServiceUnavailableError(
                f"Users service returned unexpected status: {resp.status_code}"
            )

        data = resp.json()
        return RemoteUserInfo(
            id=data["id"],
            email=data["email"],
            role=data["role"],
            status=data["status"],
            token_version=data["token_version"],
            display_name=data.get("display_name"),
        )

    def forward_request(
        self,
        method: str,
        path: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Проксира публична заявка (регистрация, вход, профил и др.) към Users сервиса."""
        url = f"{self.base_url}{path}"
        req_headers = {}
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("host", "content-length"):
                    req_headers[k] = v

        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                return client.request(
                    method=method,
                    url=url,
                    content=content,
                    headers=req_headers,
                    params=params,
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise UsersServiceUnavailableError(
                f"Users service unavailable: {type(exc).__name__}"
            ) from exc
