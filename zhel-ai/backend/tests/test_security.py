"""Тесты хеширования паролей и выпуска JWT."""

import jwt
import pytest

from src.core.config import settings
from src.core.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)


def decode(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


def test_hash_is_not_plaintext_and_uses_argon2():
    hashed = get_password_hash("secret")
    assert hashed != "secret"
    assert hashed.startswith("$argon2")


def test_hash_is_salted_so_two_hashes_differ():
    assert get_password_hash("secret") != get_password_hash("secret")


@pytest.mark.parametrize("candidate", ["secret", "Secret", "secret ", ""])
def test_verify_password_accepts_only_exact_password(candidate):
    hashed = get_password_hash("secret")
    assert verify_password(candidate, hashed) is (candidate == "secret")


def test_access_token_carries_subject_and_type():
    payload = decode(create_access_token(42))
    assert payload["sub"] == "42"
    assert payload["type"] == "access"


def test_refresh_token_is_marked_as_refresh():
    assert decode(create_refresh_token(42))["type"] == "refresh"


def test_remember_me_extends_refresh_token_lifetime():
    short = decode(create_refresh_token(42, remember_me=False))["exp"]
    long = decode(create_refresh_token(42, remember_me=True))["exp"]
    assert long > short


def test_token_signed_with_other_key_is_rejected():
    token = create_access_token(42)
    other_key = "a" * 32  # длина как у настоящего ключа, иначе PyJWT предупреждает
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other_key, algorithms=[settings.ALGORITHM])
