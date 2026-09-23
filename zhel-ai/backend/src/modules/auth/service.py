from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import BusinessError
from src.core.security import create_access_token, create_refresh_token, verify_password
from src.modules.auth.models import User
from src.modules.auth.schemas import LoginRequest, TokenResponse


async def authenticate(db: AsyncSession, data: LoginRequest) -> TokenResponse:
    """Проверяет учетные данные и выдает пару токенов.

    ``data.email`` это идентификатор пользователя: значение колонки ``users.email``.
    У демонстрационного администратора там лежит логин ``admin``, а не почта.
    """
    stmt = select(User).where(User.email == data.email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user or not verify_password(data.password, user.hashed_password):
        raise BusinessError(401, "INVALID_CREDENTIALS", "Invalid login or password")

    if not user.is_active:
        raise BusinessError(403, "USER_INACTIVE", "Account is deactivated")

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id, data.remember_me)

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)
