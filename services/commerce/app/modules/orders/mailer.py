"""SMTP клиент за изпращане на имейл известия."""

import smtplib
from email.message import EmailMessage

from app.config import settings


class EmailSender:
    """Изпращач на съобщения през SMTP сървър (напр. Mailpit локално)."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        from_email: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host or settings.smtp_host
        self.port = port or settings.smtp_port
        self.from_email = from_email or settings.smtp_from
        self.timeout = timeout

    def send_order_paid_email(
        self,
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
