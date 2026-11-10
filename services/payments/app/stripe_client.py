"""Клиент за комуникация със Stripe API (изолиран единствено в Payments сервиса)."""

from typing import Any

import stripe

from app.config import settings


class StripeNotConfigured(Exception):
    """Сигнализира, че Stripe API ключът или webhook тайната липсват в конфигурацията."""


class StripeClient:
    """Обвивка около официалния stripe.StripeClient с ограничен таймаут и опционален mock base URL в test режим."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_base: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.stripe_secret_key
        self.api_base = api_base if api_base is not None else settings.stripe_api_base
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.stripe_timeout_seconds
        )

    def _build_sdk_client(self) -> stripe.StripeClient:
        if not self.api_key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY не е конфигуриран")
        base_addresses: dict[str, str] | None = None
        if self.api_base and settings.stripe_mode == "test":
            base_addresses = {"api": self.api_base.rstrip("/")}
        http_client = stripe.RequestsClient(timeout=self.timeout_seconds)
        return stripe.StripeClient(
            api_key=self.api_key,
            base_addresses=base_addresses,  # type: ignore[arg-type]
            http_client=http_client,
        )

    def create_checkout_session(
        self,
        line_items: list[dict[str, Any]],
        success_url: str,
        cancel_url: str,
        metadata: dict[str, str],
        client_reference_id: str,
        idempotency_key: str,
    ) -> Any:
        """Създава Stripe Checkout сесия в режим 'payment' с подадения idempotency_key."""
        client = self._build_sdk_client()
        return client.v1.checkout.sessions.create(
            params={
                "mode": "payment",
                "line_items": line_items,  # type: ignore[typeddict-item]
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata,
                "client_reference_id": client_reference_id,
            },
            options={"idempotency_key": idempotency_key},
        )

    def retrieve_checkout_session(self, session_id: str) -> Any:
        """Извлича съществуваща Stripe Checkout сесия по нейното Stripe ID."""
        client = self._build_sdk_client()
        return client.v1.checkout.sessions.retrieve(session_id)

    def expire_checkout_session(self, session_id: str) -> Any:
        """Прекратява активна Stripe Checkout сесия."""
        client = self._build_sdk_client()
        return client.v1.checkout.sessions.expire(session_id)

    def construct_webhook_event(
        self,
        payload: bytes,
        sig_header: str | None,
        secret: str | None = None,
    ) -> stripe.Event:
        """Валидира криптографския подпис на Stripe webhook върху суровите байтове."""
        webhook_secret = (
            secret if secret is not None else settings.stripe_webhook_secret
        )
        if not webhook_secret:
            raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET не е конфигуриран")
        if not sig_header:
            raise ValueError("Липсва Stripe-Signature хедър")

        return stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
        )
