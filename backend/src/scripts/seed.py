"""Создание первого администратора.

В API нет регистрации, поэтому без этого скрипта получить токен невозможно.
Запуск внутри контейнера:

    docker compose exec backend python -m src.scripts.seed

Логин и пароль берутся из ``SEED_ADMIN_EMAIL`` и ``SEED_ADMIN_PASSWORD``.
Без этих переменных создается демонстрационная учетная запись ``admin`` с паролем
``admin``: стенд хакатона должен открываться у судьи с первого раза. Это осознанно
принятый риск, он описан в ``SECURITY.md`` там же, где сказано, как его убрать.
Никаких других паролей в коде нет.
"""

import asyncio
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import async_session_factory
from src.core.logger import setup_logging
from src.modules.auth.models import Role, User

logger = logging.getLogger(__name__)

DEFAULT_ROLE = "admin"
DEFAULT_EMAIL = "admin"
DEFAULT_PASSWORD = "admin"


async def get_or_create_role(session: AsyncSession, name: str) -> Role:
    role = (await session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
    if role is None:
        role = Role(name=name)
        session.add(role)
        await session.flush()
    return role


async def seed_admin(session: AsyncSession, email: str, password: str) -> bool:
    """Создает администратора. Возвращает False, если он уже был.

    Повторный запуск безопасен: существующего пользователя скрипт не трогает
    и пароль ему не переписывает.
    """
    existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        return False

    # Импорт внутри функции: hash_password тянет argon2, а он не нужен,
    # когда пользователь уже существует.
    from src.core.security import get_password_hash

    role = await get_or_create_role(session, DEFAULT_ROLE)
    session.add(
        User(
            email=email,
            hashed_password=get_password_hash(password),
            full_name="Administrator",
            role_id=role.id,
        )
    )
    await session.commit()
    return True


async def main() -> None:
    setup_logging()

    email = os.environ.get("SEED_ADMIN_EMAIL") or DEFAULT_EMAIL
    password = os.environ.get("SEED_ADMIN_PASSWORD") or DEFAULT_PASSWORD

    async with async_session_factory() as session:
        created = await seed_admin(session, email, password)

    if not created:
        logger.info("Администратор %s уже существует, ничего не менялось", email)
        return

    logger.info("Создан администратор %s", email)
    if password == DEFAULT_PASSWORD:
        logger.warning(
            "Используется демонстрационный пароль по умолчанию. Для любого стенда, кроме показа, "
            "задайте SEED_ADMIN_EMAIL и SEED_ADMIN_PASSWORD перед первым запуском скрипта."
        )


if __name__ == "__main__":
    asyncio.run(main())
