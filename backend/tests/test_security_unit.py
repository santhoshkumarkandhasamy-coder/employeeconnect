import datetime as dt

import jwt
import pytest

from app.core import security
from app.core.config import get_settings
from app.core.errors import AppError


@pytest.mark.parametrize(
    "pw,fragment",
    [
        ("Sh0rt!a", "characters"),
        ("alllowercase1!", "upper-case"),
        ("ALLUPPERCASE1!", "lower-case"),
        ("NoDigitsHere!!", "digit"),
        ("NoSymbol12345A", "symbol"),
        ("Xx1!EMP042-abc", "employee id"),
        ("Priyanka#2026x", "name"),
        ("Password1!", "too common"),
        ("A1!" + "a" * 130, "characters"),
    ],
)
def test_policy_rejects(pw, fragment):
    problems = security.validate_password(pw, "EMP042", "Priyanka Rao")
    assert any(fragment in p for p in problems), problems


def test_policy_accepts_strong_password_and_counts_unicode_chars():
    assert security.validate_password("Gr8!Tiger-Lamp", "EMP042", "Priyanka Rao") == []
    assert security.validate_password("Äb1!Äb1!", "EMP042", "x") == []  # 8 characters, not bytes


def test_temp_password_meets_policy():
    for _ in range(20):
        assert security.validate_password(security.generate_temp_password("EMP1", "A B")) == []


def test_argon2id_hash_and_verify():
    h = security.hash_password("Str0ng!Passw0rd#1")
    assert h.startswith("$argon2id$")
    assert security.verify_password(h, "Str0ng!Passw0rd#1")
    assert not security.verify_password(h, "wrong")
    assert not security.verify_password("garbage", "x")


def test_refresh_token_hash_is_sha256_hex():
    t = security.new_refresh_token()
    assert len(t) >= 64 and len(security.hash_refresh_token(t)) == 64 and security.hash_refresh_token(t) != t


def test_access_token_claims_and_15_min_expiry():
    import uuid

    uid = uuid.uuid4()
    token, expires_in = security.create_access_token(uid, "hr", False)
    assert expires_in == 900
    data = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"], audience="employeeconnect-api")
    assert data["sub"] == str(uid) and data["role"] == "hr" and data["pwd"] is False and data["jti"]
    assert data["exp"] - data["iat"] == 900
    assert "permissions" not in data


def test_jwt_rejects_none_alg_wrong_key_and_expired():
    import uuid

    uid = str(uuid.uuid4())
    now = int(dt.datetime.now(dt.UTC).timestamp())
    none_tok = jwt.encode({"sub": uid, "iat": now, "exp": now + 900}, key=None, algorithm="none")
    wrong = jwt.encode({"sub": uid, "iat": now, "exp": now + 900}, "x" * 40, algorithm="HS256")
    expired = jwt.encode({"sub": uid, "iat": now - 1000, "exp": now - 500}, get_settings().jwt_secret, algorithm="HS256")
    for tok in (none_tok, wrong):
        with pytest.raises(AppError) as e:
            security.decode_access_token(tok)
        assert e.value.code == "UNAUTHORIZED"
    with pytest.raises(AppError) as e:
        security.decode_access_token(expired)
    assert e.value.code == "TOKEN_EXPIRED"
