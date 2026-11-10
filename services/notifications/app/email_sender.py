"""SMTP адаптер за имейл известия (съвместим с Mailpit)."""

import smtplib
from email.message import EmailMessage
from typing import Any, Protocol

from app.config import settings


class EmailSenderProtocol(Protocol):
    """Протокол за изпращач на имейл известия."""

    def send_order_paid_email(
        self,
        *,
        to_email: str,
        order_id: int,
        total_minor: int,
        currency: str,
    ) -> None: ...

    def send_shipment_shipped_email(
        self,
        *,
        to_email: str,
        order_id: int,
        shipment_id: int,
        items: list[dict[str, Any]],
    ) -> None: ...


class EmailSender:
    """Изпращач на имейли през SMTP (напр. Mailpit локално)."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        from_email: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.host = host or settings.smtp_host
        self.port = port or settings.smtp_port
        self.from_email = from_email or settings.smtp_from
        self.timeout = timeout if timeout is not None else settings.smtp_timeout_seconds
        if self.timeout <= 0:
            raise ValueError("SMTP timeout must be positive")

    def send_order_paid_email(
        self,
        *,
        to_email: str,
        order_id: int,
        total_minor: int,
        currency: str,
    ) -> None:
        """Изпраща потвърждение за платена поръчка с номер, сума и валута."""
        formatted_amount = f"{total_minor / 100:.2f}"

        msg = EmailMessage()
        msg["Subject"] = f"Потвърждение за платена поръчка #{order_id}"
        msg["From"] = self.from_email
        msg["To"] = to_email

        body = (
            f"Здравейте,\n\n"
            f"Вашата поръчка беше успешно платена!\n\n"
            f"Детайли за поръчката:\n"
            f"- Номер на поръчка: #{order_id}\n"
            f"- Обща сума: {formatted_amount} {currency} ({total_minor} цента)\n"
            f"- Валута: {currency}\n\n"
            f"Благодарим ви, че пазарувахте при нас!\n"
        )
        msg.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            client.send_message(msg)

    def send_shipment_shipped_email(
        self,
        *,
        to_email: str,
        order_id: int,
        shipment_id: int,
        items: list[dict[str, Any]],
    ) -> None:
        """Изпраща потвърждение за изпратена пратка със списък на включените позиции."""
        items_lines = []
        for it in items:
            title = it.get("product_title", "")
            qty = it.get("quantity", 1)
            items_lines.append(f"- {title} ({qty} бр.)")
        items_text = "\n".join(items_lines)

        msg = EmailMessage()
        msg["Subject"] = (
            f"Вашата пратка #{shipment_id} от поръчка #{order_id} е изпратена"
        )
        msg["From"] = self.from_email
        msg["To"] = to_email

        body = (
            f"Здравейте,\n\n"
            f"Част от вашата поръчка беше успешно изпратена!\n\n"
            f"Детайли за пратката:\n"
            f"- Номер на поръчка: #{order_id}\n"
            f"- Номер на пратка: #{shipment_id}\n\n"
            f"Изпратени стоки:\n"
            f"{items_text}\n\n"
            f"Благодарим ви, че пазарувахте при нас!\n"
        )
        msg.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            client.send_message(msg)
