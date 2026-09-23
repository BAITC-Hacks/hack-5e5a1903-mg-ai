from typing import Literal

from pydantic import PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    ENVIRONMENT: Literal["local", "dev", "production"] = "local"

    BACKEND_CORS_ORIGINS: list[str] = ["*"]

    POSTGRES_HOST: str
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str

    DEBUG: bool = False

    @computed_field
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    # Соседние сервисы: модель (dev2) и погода (dev3). Вызов всегда с таймаутом,
    # отказ превращается в BusinessError, а не в 500.
    ML_SERVICE_URL: str = "http://ml:8000"
    WEATHER_SERVICE_URL: str = "http://weather:8000"
    WEATHER_SERVICE_TIMEOUT: float = 15.0
    ML_SERVICE_TIMEOUT: float = 30.0

    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30


settings = Settings()
