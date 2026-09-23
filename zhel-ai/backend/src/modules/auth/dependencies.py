import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db
from src.core.exceptions import BusinessError
from src.modules.auth.models import User

security_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            raise BusinessError(401, "INVALID_TOKEN", "Invalid token type")
        user_id = int(payload["sub"])
    except jwt.ExpiredSignatureError as e:
        raise BusinessError(401, "TOKEN_EXPIRED", "Token expired") from e
    except (jwt.InvalidTokenError, KeyError, ValueError) as e:
        raise BusinessError(401, "INVALID_TOKEN", "Invalid token") from e

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise BusinessError(401, "USER_NOT_FOUND", "User not found")
    if not user.is_active:
        raise BusinessError(403, "USER_INACTIVE", "Account is deactivated")

    return user
