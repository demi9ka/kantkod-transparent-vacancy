import logging
import re
from logging.handlers import RotatingFileHandler

from app.config import Settings


class SecretFilter(logging.Filter):
    def __init__(self, auth_key: str):
        super().__init__()
        self.auth_key = auth_key

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.auth_key:
            message = message.replace(self.auth_key, "[REDACTED]")
        message = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_+/=.\-]+", r"\1 [REDACTED]", message)
        message = re.sub(
            r'(?i)("(?:authorization|access_token|refresh_token|api_key|gigachat_auth_key)"\s*:\s*)"(?:\\.|[^"\\])*"',
            r'\1"[REDACTED]"', message,
        )
        record.msg, record.args = message, ()
        return True


def configure_logging(settings: Settings) -> None:
    """Настраивает только логи приложения, не включая DEBUG сторонних HTTP-библиотек."""
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setLevel(settings.log_level)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(settings.log_console_level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret_filter = SecretFilter(settings.gigachat_auth_key.get_secret_value().strip())
    logger = logging.getLogger("app")
    for handler in list(logger.handlers):
        if getattr(handler, "_vacancy_handler", False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(min(file_handler.level, console_handler.level))
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler.addFilter(secret_filter)
        handler._vacancy_handler = True
        logger.addHandler(handler)
