from typing import ClassVar, Literal


class UnsupportedPaymentMethod(Exception):
    """Сигнализира за неподдържан фиктивен метод на плащане."""


class PaymentSimulator:
    """Локален симулатор на резултати от плащане без мрежови заявки."""

    SUPPORTED_METHODS: ClassVar[dict[str, Literal["succeeded", "declined"]]] = {
        "test_success": "succeeded",
        "test_declined": "declined",
    }

    def simulate(self, payment_method: str) -> Literal["succeeded", "declined"]:
        """Определя резултата от опита за плащане на база фиктивния метод."""
        result = self.SUPPORTED_METHODS.get(payment_method)
        if result is None:
            raise UnsupportedPaymentMethod(
                f"Payment method '{payment_method}' is not supported"
            )
        return result
