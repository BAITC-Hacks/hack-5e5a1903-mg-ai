from fastapi import Depends

from src.core.exceptions import BusinessError
from src.modules.auth.dependencies import get_current_user
from src.modules.auth.models import User

_ADMIN = 1
_TECH = 2
_MANAGER = 3

_ADMIN_ROLES = {_ADMIN}
_TECH_ROLES = {_ADMIN, _TECH}


async def require_admin(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> User:
    if current_user.role_id not in _ADMIN_ROLES:
        raise BusinessError(403, "FORBIDDEN", "Admin role required")
    return current_user


async def require_tech(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> User:
    if current_user.role_id not in _TECH_ROLES:
        raise BusinessError(403, "FORBIDDEN", "Tech or admin role required")
    return current_user
