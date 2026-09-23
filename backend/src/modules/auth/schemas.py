from pydantic import AliasChoices, Field

from src.core.base_schemas import BaseAppSchema


class LoginRequest(BaseAppSchema):
    """Тело запроса на вход.

    Поле ``email`` это идентификатор пользователя, а не обязательно почта:
    демонстрационный администратор входит по логину ``admin``. Ключ ``username``
    принимается как псевдоним, чтобы интерфейс мог слать привычное имя поля,
    а старый контракт с ключом ``email`` продолжал работать.
    """

    email: str = Field(validation_alias=AliasChoices("email", "username"))
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
