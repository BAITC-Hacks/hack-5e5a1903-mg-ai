"""Тесты скрипта создания первого администратора."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import BusinessError
from src.modules.auth.models import Role, User
from src.modules.auth.schemas import LoginRequest
from src.modules.auth.service import authenticate
from src.scripts.seed import DEFAULT_EMAIL, DEFAULT_PASSWORD, seed_admin


async def count(session: AsyncSession, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_seed_creates_admin_who_can_log_in(session: AsyncSession):
    created = await seed_admin(session, "admin@hackalem.local", "seed-password")

    assert created is True
    tokens = await authenticate(session, LoginRequest(email="admin@hackalem.local", password="seed-password"))
    assert tokens.access_token


async def test_seed_is_idempotent(session: AsyncSession):
    await seed_admin(session, "admin@hackalem.local", "seed-password")

    created_again = await seed_admin(session, "admin@hackalem.local", "other-password")

    assert created_again is False
    assert await count(session, User) == 1
    assert await count(session, Role) == 1


async def test_second_admin_reuses_the_existing_role(session: AsyncSession):
    await seed_admin(session, "first@hackalem.local", "pw")

    await seed_admin(session, "second@hackalem.local", "pw")

    assert await count(session, User) == 2
    assert await count(session, Role) == 1


async def test_defaults_are_the_demo_admin_admin_pair(session: AsyncSession):
    """Стенд для показа: без переменных окружения получается admin/admin."""
    created = await seed_admin(session, DEFAULT_EMAIL, DEFAULT_PASSWORD)

    assert created is True
    assert (DEFAULT_EMAIL, DEFAULT_PASSWORD) == ("admin", "admin")
    tokens = await authenticate(session, LoginRequest(username=DEFAULT_EMAIL, password=DEFAULT_PASSWORD))
    assert tokens.access_token


async def test_repeated_seed_keeps_the_original_password(session: AsyncSession):
    """Идемпотентность: второй запуск не перезаписывает пароль существующего админа."""
    await seed_admin(session, DEFAULT_EMAIL, DEFAULT_PASSWORD)

    assert await seed_admin(session, DEFAULT_EMAIL, "another-password") is False

    tokens = await authenticate(session, LoginRequest(email=DEFAULT_EMAIL, password=DEFAULT_PASSWORD))
    assert tokens.access_token
    with pytest.raises(BusinessError):
        await authenticate(session, LoginRequest(email=DEFAULT_EMAIL, password="another-password"))
