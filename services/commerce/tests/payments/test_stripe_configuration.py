from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.main import app
from app.modules.orders.dependencies import get_stripe_client
from app.modules.orders.stripe_client import StripeClient
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_stripe_keys_not_leaked_in_errors(db_session: Session) -> None:
    """Проверка, че Stripe secret keys никога не присъстват в съобщенията за грешка."""
    secret_value = "sk_test_super_secret_key_123456789"
    webhook_secret = "whsec_super_secret_webhook_987654321"

    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Leak Test", price_minor=1000)
    order = _create_order(db_session, user.id, product)

    with (
        patch.object(settings, "stripe_secret_key", secret_value),
        patch.object(settings, "stripe_webhook_secret", webhook_secret),
    ):
        mock_client = MagicMock(spec=StripeClient)
        mock_client.create_checkout_session.side_effect = Exception(
            f"Stripe internal server issue with key {secret_value}"
        )
        app.dependency_overrides[get_stripe_client] = lambda: mock_client
        try:
            resp = client.post(
                f"/orders/{order.id}/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp_text = resp.text
            assert secret_value not in resp_text
            assert webhook_secret not in resp_text
        finally:
            app.dependency_overrides.pop(get_stripe_client, None)


def test_config_stripe_mode_validation() -> None:
    """Валидира настройките за STRIPE_MODE (test vs live) в Settings."""
    # 1. Live режим изисква sk_live_...
    with pytest.raises(
        ValidationError,
        match="In live mode, STRIPE_SECRET_KEY must start with 'sk_live_'",
    ):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_test_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="https://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 2. Live режим изисква webhook_secret
    with pytest.raises(
        ValidationError, match="In live mode, STRIPE_WEBHOOK_SECRET must be set"
    ):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret=None,
            stripe_success_url="https://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 3. Live режим изисква https URL
    with pytest.raises(ValidationError, match="must start with 'https://'"):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="http://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 4. Test режим не позволява live ключ (защита от грешка)
    with pytest.raises(
        ValidationError, match="Live key .* cannot be used in test mode"
    ):
        Settings(
            stripe_mode="test",
            stripe_secret_key="sk_live_12345",
        )
