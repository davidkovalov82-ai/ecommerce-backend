"""Адаптер за SMS провайдър (Twilio REST контракт с безопасен тестов режим)."""

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.config import settings


class SmsDeliveryError(Exception):
    """Грешка при доставка на SMS (без включване на телефонни номера или тайни)."""


@dataclass(frozen=True)
class SmsSendResult:
    """Резултат от прието SMS съобщение от провайдъра."""

    provider: str
    provider_message_id: str
    status: str


class SmsSenderProtocol(Protocol):
    """Протокол за SMS провайдър."""

    def send_sms(
        self,
        *,
        to_phone: str,
        message: str,
        idempotency_key: str,
    ) -> SmsSendResult: ...


def format_order_paid_sms(*, order_id: int, total_minor: int, currency: str) -> str:
    """Форматира кратко SMS за платена поръчка без лични данни."""
    return f"Поръчка #{order_id} е успешно платена: {total_minor / 100:.2f} {currency}."


def format_shipment_shipped_sms(
    *, order_id: int, shipment_id: int, items_count: int
) -> str:
    """Форматира кратко SMS за изпратена пратка."""
    return (
        f"Пратка #{shipment_id} от поръчка #{order_id} "
        f"({items_count} поз.) е изпратена."
    )


class TwilioSmsProvider:
    """Адаптер към Twilio Messages API (POST /2010-04-01/Accounts/{sid}/Messages.json).

    - В mode='test' без зададен sms_api_base_url и транспорт не прави платени външни заявки
      и връща синтетичен идентификатор на приемане.
    - В mode='test' със зададен локален sms_api_base_url или mock transport изпълнява
      истинска HTTP POST заявка по контракта на Twilio (идеално за интеграционни и
      контрактни тестове без реални разходи).
    - В mode='live' използва реалния HTTPS API на Twilio.
    """

    def __init__(
        self,
        *,
        mode: Literal["test", "live"] | None = None,
        api_base_url: str | None = None,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.mode = mode or settings.sms_mode
        self.api_base_url = (
            api_base_url
            if api_base_url is not None
            else (settings.sms_api_base_url or "")
        ).rstrip("/")
        self.account_sid = account_sid or settings.sms_account_sid
        self.auth_token = auth_token or settings.sms_auth_token
        self.from_number = from_number or settings.sms_from_number
        self.timeout = timeout if timeout is not None else settings.sms_timeout_seconds
        self._transport = transport

    def send_sms(
        self,
        *,
        to_phone: str,
        message: str,
        idempotency_key: str,
    ) -> SmsSendResult:
        """Изпраща SMS по Twilio договора или в локален тестов режим."""
        cleaned_phone = to_phone.strip()
        if not cleaned_phone:
            raise SmsDeliveryError("empty_recipient_phone")

        if self.mode == "test" and not self.api_base_url and self._transport is None:
            return SmsSendResult(
                provider="twilio_test",
                provider_message_id=f"SM_TEST_{uuid.uuid4().hex[:24]}",
                status="test_accepted",
            )

        base_url = self.api_base_url or "https://api.twilio.com"
        endpoint = f"{base_url}/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        client_kwargs: dict[str, object] = {"timeout": self.timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        try:
            with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
                response = client.post(
                    endpoint,
                    auth=(self.account_sid, self.auth_token),
                    data={
                        "To": cleaned_phone,
                        "From": self.from_number,
                        "Body": message,
                    },
                    headers={"Idempotency-Key": idempotency_key},
                )
        except httpx.HTTPError as exc:
            raise SmsDeliveryError(
                f"twilio_network_error:{type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            error_code = None
            try:
                err_json = response.json()
                if isinstance(err_json, dict):
                    error_code = err_json.get("code")
            except ValueError:
                pass
            suffix = f":code={error_code}" if error_code is not None else ""
            raise SmsDeliveryError(f"twilio_http_{response.status_code}{suffix}")

        try:
            data = response.json()
        except ValueError as exc:
            raise SmsDeliveryError("twilio_invalid_json_response") from exc

        if not isinstance(data, dict) or not data.get("sid"):
            raise SmsDeliveryError("twilio_missing_message_sid")

        return SmsSendResult(
            provider="twilio",
            provider_message_id=str(data["sid"]),
            status=str(data.get("status") or "queued"),
        )
