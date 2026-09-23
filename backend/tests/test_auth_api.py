"""Тесты HTTP-слоя: коды ответов и единый конверт ошибок."""

from httpx import AsyncClient

from src.core.security import create_access_token, create_refresh_token
from src.modules.auth.models import User
from tests.conftest import TEST_PASSWORD


async def login(client: AsyncClient, email: str, password: str):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def test_login_returns_token_pair(client: AsyncClient, user: User):
    response = await login(client, user.email, TEST_PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]


async def test_login_with_wrong_password_uses_error_envelope(client: AsyncClient, user: User):
    response = await login(client, user.email, "wrong")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_missing_field_reports_the_field_name(client: AsyncClient):
    response = await client.post("/api/auth/login", json={"email": "a@b.c"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"password": "Field required"}


async def test_me_returns_current_user_with_role(client: AsyncClient, user: User):
    token = create_access_token(user.id)

    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == user.email
    assert body["role"]["name"] == "admin"
    assert "hashed_password" not in body


async def test_me_without_token_is_unauthorized(client: AsyncClient, user: User):
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_refresh_token_is_not_accepted_as_access_token(client: AsyncClient, user: User):
    token = create_refresh_token(user.id)

    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_malformed_token_is_rejected(client: AsyncClient, user: User):
    response = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_token_of_deleted_user_is_rejected(client: AsyncClient, user: User):
    token = create_access_token(user.id + 999)

    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"
