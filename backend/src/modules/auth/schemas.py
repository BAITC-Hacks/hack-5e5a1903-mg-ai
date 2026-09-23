from src.core.base_schemas import BaseAppSchema


class LoginRequest(BaseAppSchema):
    email: str
    password: str
    remember_me: bool = False


class TokenResponse(BaseAppSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseAppSchema):
    refresh_token: str


class RoleRead(BaseAppSchema):
    id: int
    name: str


class UserRead(BaseAppSchema):
    id: int
    email: str
    full_name: str
    is_active: bool
    role: RoleRead
