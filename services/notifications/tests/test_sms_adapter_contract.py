"""Контрактни тестове на Twilio SMS адаптера без реални платени изпращания."""

import base64
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings
from app.sms_sender import SmsDeliveryError, TwilioSmsProvider


def test_twilio_sms_adapter_contract_request_and_response() -> None:
    """Проверява точния HTTP контракт към Twilio Messages API без реална мрежа."""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201,
            json={"sid": "SM1234567890abcdef", "status": "queued"},
        )

    provider = TwilioSmsProvider(
        mode="test",
        api_base_url="https://twilio.mock.local",
        account_sid="ACtestaccountsid123",
        auth_token="secret-token-xyz",
        from_number="+359888000111",
        transport=httpx.MockTransport(_handler),
    )

    res = provider.send_sms(
        to_phone="+359888999000",
        message="Order #42 paid",
        idempotency_key="evt-42:sms",
    )

    assert res.provider == "twilio"
    assert res.provider_message_id == "SM1234567890abcdef"
    assert res.status == "queued"

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert (
        str(req.url)
        == "https://twilio.mock.local/2010-04-01/Accounts/ACtestaccountsid123/Messages.json"
    )
    assert req.headers["Idempotency-Key"] == "evt-42:sms"

    expected_basic = base64.b64encode(b"ACtestaccountsid123:secret-token-xyz").decode()
    assert req.headers["Authorization"] == f"Basic {expected_basic}"

    form = parse_qs(req.content.decode("utf-8"))
    assert form["To"] == ["+359888999000"]
    assert form["From"] == ["+359888000111"]
    assert form["Body"] == ["Order #42 paid"]


def test_twilio_sms_adapter_redacts_pii_and_secrets_on_error() -> None:
    """При 4xx/5xx от провайдъра грешката не разкрива телефонния номер или токена."""

    def _fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 21211, "message": "Invalid phone"})

    provider = TwilioSmsProvider(
        mode="test",
        api_base_url="https://twilio.mock.local",
        account_sid="ACtest",
        auth_token="super-secret-token",
        from_number="+359888000111",
        transport=httpx.MockTransport(_fail_handler),
    )

    with pytest.raises(SmsDeliveryError) as exc_info:
        provider.send_sms(
            to_phone="+359888123987",
            message="Test",
            idempotency_key="k1",
        )

    err_msg = str(exc_info.value)
    assert "twilio_http_400:code=21211" == err_msg
    assert "+359888123987" not in err_msg
    assert "super-secret-token" not in err_msg


def test_twilio_sms_test_mode_avoids_paid_calls_and_live_mode_validates_config() -> (
    None
):
    """Тестовият режим не прави платени заявки, а live режимът изисква реални HTTPS настройки."""
    provider = TwilioSmsProvider(mode="test", api_base_url="")
    res = provider.send_sms(
        to_phone="+359888111222",
        message="Order #1 paid",
        idempotency_key="evt-1:sms",
    )
    assert res.provider == "twilio_test"
    assert res.status == "test_accepted"
    assert res.provider_message_id.startswith("SM_TEST_")

    with pytest.raises(ValueError, match="https://"):
        Settings(sms_mode="live", sms_api_base_url="http://insecure.example.com")
