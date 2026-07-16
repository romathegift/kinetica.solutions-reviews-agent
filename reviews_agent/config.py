"""
Конфигурация проекта.

Единая точка чтения настроек из .env для всех модулей.
Секреты обёрнуты в SecretStr — они не утекут в логи, print() и трейсбеки.
"""

from functools import lru_cache
from pathlib import Path

from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта: reviews_agent/config.py -> reviews_agent/ -> корень репозитория
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Абсолютный путь к .env.
# Важно: pydantic-settings ищет env_file относительно текущей рабочей директории,
# а не относительно модуля. Абсолютный путь убирает сюрпризы при запуске
# скриптов из подпапок (например, из scripts/).
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Настройки приложения. Имена полей = ключи .env (регистр не важен)."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",  # лишние ключи в .env не роняют старт приложения
    )

    # --- Postgres (база reviews на VPS, ходим через SSH-туннель) ---
    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: SecretStr

    # --- Anthropic (Haiku 4.5 — классификация, Sonnet 5 — генерация) ---
    anthropic_api_key: SecretStr

    # --- Telegram (HITL-гейт: карточки approve/reject/edit) ---
    telegram_bot_token: SecretStr
    telegram_chat_id: int

    # --- LangGraph ---
    # Требование безопасности langgraph-checkpoint-postgres 3.1.0:
    # строгая msgpack-сериализация чекпоинтов.
    langgraph_strict_msgpack: bool = True

    @property
    def postgres_kwargs(self) -> dict[str, str | int]:
        """Параметры подключения к Postgres словарём (пароль в открытом виде — он нужен драйверу)."""
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "dbname": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password.get_secret_value(),
        }

    @property
    def postgres_dsn(self) -> str:
        """
        Строка подключения (conninfo) для psycopg и PostgresSaver.

        Собирается через make_conninfo, а не руками: он сам экранирует
        спецсимволы. Ручная сборка URL вида postgresql://user:pass@host
        сломалась бы на пароле с символами @ : / ? #.
        """
        return make_conninfo(**self.postgres_kwargs)


@lru_cache
def get_settings() -> Settings:
    """
    Singleton настроек.

    lru_cache гарантирует, что .env читается один раз за процесс,
    а все модули получают один и тот же объект.
    """
    return Settings()