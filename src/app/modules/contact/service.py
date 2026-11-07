import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.errors import bad_request
from app.modules.contact.schemas import ContactRequest


def _clean_header(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _looks_like_email(value: str) -> bool:
    return "@" in value and "." in value.rsplit("@", 1)[-1]


async def send_contact_request(payload: ContactRequest) -> None:
    settings = get_settings()
    recipient = (settings.contact_email_to or settings.smtp_username or settings.auth_email_from).strip()
    if not settings.smtp_host or not recipient:
        raise bad_request("Почта для обращений пока не настроена.")

    created_at = datetime.now(UTC).strftime("%d.%m.%Y %H:%M UTC")
    message = EmailMessage()
    message["Subject"] = "Новое обращение demo-platform"
    message["From"] = settings.auth_email_from
    message["To"] = recipient
    if _looks_like_email(payload.contact):
        message["Reply-To"] = _clean_header(payload.contact)

    message.set_content(
        "\n".join(
            [
                "Новое обращение с сайта demo-platform.",
                "",
                f"Время: {created_at}",
                f"Имя: {payload.name.strip()}",
                f"Контакт: {payload.contact.strip()}",
                f"Страница: {payload.page_url or '-'}",
                "",
                "Сообщение:",
                payload.message.strip(),
            ]
        )
    )

    try:
        smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls and not settings.smtp_use_ssl:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except Exception as exc:
        raise bad_request("Не удалось отправить обращение. Попробуйте еще раз или напишите на почту.") from exc
