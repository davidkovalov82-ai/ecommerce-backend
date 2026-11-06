from typing import Any

import stripe

from app.config import settings


class StripeNotConfigured(Exception):
    """Сигнализира, че Stripe API ключът или webhook тайната липсват в конфигурацията."""


class StripeClient:
    """Клиент за комуникация със Stripe API чрез официалния Python SDK."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.stripe_secret_key

    def create_checkout_session(
        self,
        line_items: list[dict[str, Any]],
        success_url: str,
        cancel_url: str,
        metadata: dict[str, str],
        client_reference_id: str,
        idempotency_key: str,
    ) -> stripe.checkout.Session:
        """Създава Stripe Checkout сесия в режим 'payment' само за карти."""
        if not self.api_key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY не е конфигуриран")

        return stripe.checkout.Session.create(
            api_key=self.api_key,
            mode="payment",
            payment_method_types=["card"],
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            client_reference_id=client_reference_id,
            idempotency_key=idempotency_key,
        )

    def retrieve_checkout_session(self, session_id: str) -> stripe.checkout.Session:
        """Извлича съществуваща Stripe Checkout сесия по нейното ID."""
        if not self.api_key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY не е конфигуриран")

        return stripe.checkout.Session.retrieve(
            session_id,
            api_key=self.api_key,
        )

    def expire_checkout_session(self, session_id: str) -> stripe.checkout.Session:
        """Прекратява активна Stripe Checkout сесия."""
        if not self.api_key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY не е конфигуриран")

        return stripe.checkout.Session.expire(
            session_id,
            api_key=self.api_key,
        )

    def construct_webhook_event(
        self,
        payload: bytes,
        sig_header: str | None,
        secret: str | None = None,
    ) -> stripe.Event:
        """Валидира подписа на полученото Stripe webhook събитие върху суровите байтове."""
        webhook_secret = secret or settings.stripe_webhook_secret
        if not webhook_secret:
            raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET не е конфигуриран")
        if not sig_header:
            raise ValueError("Липсва Stripe-Signature хедър")

        return stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
        )

