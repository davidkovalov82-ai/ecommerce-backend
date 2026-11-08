"""SMTP клиент за изпращане на имейл известия за акаунти."""

import smtplib
from email.message import EmailMessage

from app.config import settings


class AccountMailer:
    """Изпращач на имейли за акаунт събития (напр. възстановяване на парола) през SMTP / Mailpit."""

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

    def send_password_reset_email(
        self,
        to_email: str,
        token: str,
        reset_url: str,
    ) -> None:
        """Изпраща имейл с линк и еднократен токен за възстановяване на парола."""
        msg = EmailMessage()
        msg["Subject"] = "Възстановяване на парола"
        msg["From"] = self.from_email
        msg["To"] = to_email

        body = (
            f"Здравейте,\n\n"
            f"Получихме заявка за възстановяване на паролата за вашия акаунт.\n"
            f"За да завършите процеса, използвайте следния еднократен токен или линк:\n\n"
            f"Токен: {token}\n"
            f"Линк: {reset_url}\n\n"
            f"Токенът е валиден 15 минути и може да бъде използван само веднъж.\n"
            f"Ако не сте заявявали промяна на паролата, моля игнорирайте това съобщение.\n"
        )
        msg.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            client.send_message(msg)
