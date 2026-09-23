"""Тесты сервиса аутентификации: бизнес-логика без HTTP-слоя."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import BusinessError
from src.modules.auth.models import User
from src.modules.auth.schemas import LoginRequest
from src.modules.auth.service import authenticate
from tests.conftest import TEST_PASSWORD


async def test_valid_credentials_return_both_tokens(session: AsyncSession, user: User):
    tokens = await authenticate(session, LoginRequest(email=user.email, password=TEST_PASSWORD))

    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "bearer"


async def test_wrong_password_is_rejected(session: AsyncSession, user: User):
    with pytest.raises(BusinessError) as err:
        await authenticate(session, LoginRequest(email=user.email, password="wrong"))

    assert err.value.status_code == 401
    assert err.value.code == "INVALID_CREDENTIALS"


async def test_unknown_email_gives_the_same_error_as_wrong_password(session: AsyncSession, user: User):
    """Ответ не должен подсказывать, существует ли такой email."""
    with pytest.raises(BusinessError) as err:
        await authenticate(session, LoginRequest(email="nobody@hackalem.local", password=TEST_PASSWORD))

    assert err.value.status_code == 401
    assert err.value.code == "INVALID_CREDENTIALS"


async def test_deactivated_user_cannot_log_in(session: AsyncSession, user: User):
    user.is_active = False
    await session.commit()

    with pytest.raises(BusinessError) as err:
        await authenticate(session, LoginRequest(email=user.email, password=TEST_PASSWORD))

    assert err.value.status_code == 403
    assert err.value.code == "USER_INACTIVE"
