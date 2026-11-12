"""Конфигурация приложения: загрузка и проверка переменных окружения."""

import os
from dotenv import load_dotenv

# Загружаем переменные из файла .env в корне проекта
load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
YANDEX_TOKEN: str = os.getenv("YANDEX_TOKEN", "").strip()

# VK пока отключим (сделаем необязательными), чтобы бот запускался без них
VK_TOKEN: str = os.getenv("VK_TOKEN", "").strip()
VK_USER_ID: str = os.getenv("VK_USER_ID", "").strip()
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

# Добавь вот эту строку:
STREAM_SECRET_KEY: str = os.getenv("STREAM_SECRET_KEY", TELEGRAM_BOT_TOKEN).strip()


def validate_config() -> None:
    """Проверяет, что ОБЯЗАТЕЛЬНЫЕ токены заданы."""
    missing: list[str] = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not YANDEX_TOKEN:
        missing.append("YANDEX_TOKEN")
    
    # Проверку VK мы убрали, так как пока используем YouTube и Яндекс

    if missing:
        names = ", ".join(missing)
        raise ValueError(
            f"Не заданы обязательные переменные окружения: {names}. "
            "Проверьте файл .env"
        )

# Проверяем конфигурацию при импорте модуля
validate_config()