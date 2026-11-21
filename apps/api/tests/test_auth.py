"""The Supabase token check on API routes (ADR-024), with tokens signed by a test key pair."""

import time
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

from agentspace.auth import SigningKeys, TokenVerifier
from agentspace.config import Settings
from agentspace.main import create_app

SUPABASE_URL = "https://test.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"

# The "Supabase" key pair: tokens are signed with the private half, the API only sees the public one.
PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


class FixedKeys:
    """Stands in for the JWKS endpoint: always the test key's public half."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        return jwt.PyJWK(ECAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True), "ES256")


class UnreachableKeys:
    """A JWKS endpoint that can't be reached."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        raise jwt.PyJWKClientConnectionError("connection refused")


def make_token(key: ec.EllipticCurvePrivateKey = PRIVATE_KEY, **claims: object) -> str:
    """A token like Supabase's, valid for an hour unless `claims` override it."""
    now = int(time.time())
    payload = {"sub": "user-1", "email": "a@example.com", "aud": "authenticated", "iss": ISSUER, "exp": now + 3600}
    return jwt.encode(payload | claims, key, algorithm="ES256")


def signed_in_app(tmp_path: Path, keys: SigningKeys) -> TestClient:
    """A client for an app that checks tokens against `keys`."""
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, supabase_url=SUPABASE_URL))
    app.state.auth = TokenVerifier(SUPABASE_URL, keys=keys)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with signed_in_app(tmp_path, FixedKeys()) as client:
        yield client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_open(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_a_valid_token_is_let_in(client: TestClient) -> None:
    assert client.get("/sessions", headers=auth(make_token())).status_code == 200


def test_no_token_is_refused(client: TestClient) -> None:
    response = client.get("/sessions")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "Sign in to use agentspace."


def test_an_expired_token_says_so(client: TestClient) -> None:
    response = client.get("/sessions", headers=auth(make_token(exp=int(time.time()) - 10)))
    assert response.status_code == 401
    assert response.json()["detail"] == "Your session expired. Sign in again."


@pytest.mark.parametrize(
    "token",
    [
        make_token(key=OTHER_KEY),  # forged: signed by someone else
        make_token(aud="anon"),  # not a signed-in user's token
        make_token(iss="https://other.supabase.co/auth/v1"),  # another Supabase project
        jwt.encode({"sub": "user-1", "aud": "authenticated", "iss": ISSUER}, PRIVATE_KEY, algorithm="ES256"),  # no exp
        "not-a-jwt",
    ],
)
def test_a_bad_token_is_refused(client: TestClient, token: str) -> None:
    response = client.get("/sessions", headers=auth(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Your sign-in isn't valid. Sign in again."


def test_unreachable_keys_are_a_503_not_a_401(tmp_path: Path) -> None:
    # A 401 would sign the user out for our outage.
    with signed_in_app(tmp_path, UnreachableKeys()) as client:
        assert client.get("/sessions", headers=auth(make_token())).status_code == 503


def test_local_mode_needs_no_token(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        assert client.get("/sessions").status_code == 200
