import time

import jwt
import pytest

from p2pilot.auth import AuthError, bearer_token, verify_token

SECRET = "test-secret-with-at-least-32-characters!!"


def make_token(**overrides) -> str:
    claims = {
        "sub": "user-123",
        "email": "clerk@acme.test",
        "aud": "authenticated",
        "exp": int(time.time()) + 300,
        "role": "authenticated",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}  # None = leave the claim out
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_valid_token_returns_principal():
    p = verify_token(make_token(), audience="authenticated", hs256_secret=SECRET)
    assert p.user_id == "user-123"
    assert p.email == "clerk@acme.test"


def test_expired_token_is_rejected():
    with pytest.raises(AuthError, match="expired"):
        verify_token(make_token(exp=int(time.time()) - 10), audience="authenticated",
                     hs256_secret=SECRET)


def test_wrong_audience_is_rejected():
    with pytest.raises(AuthError, match="audience"):
        verify_token(make_token(aud="anon"), audience="authenticated", hs256_secret=SECRET)


def test_wrong_signature_is_rejected():
    forged = jwt.encode({"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
                        "another-secret-of-sufficient-length-123", algorithm="HS256")
    with pytest.raises(AuthError, match="Invalid token"):
        verify_token(forged, audience="authenticated", hs256_secret=SECRET)


def test_alg_none_is_rejected():
    unsigned = jwt.encode({"sub": "x", "aud": "authenticated"}, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verify_token(unsigned, audience="authenticated", hs256_secret=SECRET)


def test_token_without_subject_is_rejected():
    with pytest.raises(AuthError, match="subject"):
        verify_token(make_token(sub=None), audience="authenticated", hs256_secret=SECRET)


def test_no_key_configured_fails_closed():
    with pytest.raises(AuthError, match="no JWT verification key"):
        verify_token(make_token(), audience="authenticated")


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   "])
def test_bad_authorization_headers(header):
    with pytest.raises(AuthError):
        bearer_token(header)


def test_bearer_token_extracts_token():
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
