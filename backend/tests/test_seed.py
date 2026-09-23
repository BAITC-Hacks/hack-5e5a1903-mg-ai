"""Тесты скрипта создания первого администратора."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.auth.models import Role, User
from src.modules.auth.schemas import LoginRequest
from src.modules.auth.service import authenticate
from src.scripts.seed import seed_admin


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
