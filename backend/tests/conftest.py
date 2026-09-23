"""Общие фикстуры тестов.

Переменные окружения выставляются до импорта ``src``, потому что
``src.core.config.Settings`` читает их на этапе импорта модуля и без них падает.
"""

import os

os.environ.update(
    {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "test",
        "POSTGRES_PASSWORD": "test",
        "POSTGRES_DB": "test",
        "ENVIRONMENT": "local",
        "DEBUG": "False",
        "SECRET_KEY": "test-secret-key-not-used-anywhere-else",
    }
)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.core.database import Base, get_db  # noqa: E402
from src.core.security import get_password_hash  # noqa: E402
from src.main import app  # noqa: E402
from src.modules.auth.models import Role, User  # noqa: E402

TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
async def session() -> AsyncSession:
    """Чистая БД на каждый тест: sqlite в памяти на одном соединении."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture
async def user(session: AsyncSession) -> User:
    """Активный пользователь с ролью admin и известным паролем."""
    role = Role(name="admin")
    session.add(role)
    await session.flush()

    u = User(
        email="dev1@hackalem.local",
        hashed_password=get_password_hash(TEST_PASSWORD),
        full_name="Dev One",
        role_id=role.id,
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest.fixture
async def client(session: AsyncSession) -> AsyncClient:
    """HTTP-клиент поверх приложения с подменой сессии БД на тестовую.

    ASGITransport не выполняет lifespan, поэтому обращения к настоящему
    PostgreSQL на старте приложения не происходит.
    """

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
