"""Checks the Supabase access token on each API request (ADR-024)."""

from dataclasses import dataclass
from typing import Annotated, Protocol

import jwt
from asyncer import asyncify
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


@dataclass(frozen=True)
class AuthUser:
    """The signed-in user a token belongs to."""

    id: str
    """Supabase's user id (the token's `sub`)."""
    email: str | None


# TS: like an interface — any class with this method fits, no "implements" needed.
class SigningKeys(Protocol):
    """Finds the public key a token was signed with. `PyJWKClient` in production, a fixed key in tests."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK: ...


class TokenVerifier:
    """Verifies Supabase access tokens locally against the project's public keys (JWKS)."""

    def __init__(self, supabase_url: str, keys: SigningKeys | None = None) -> None:
        self.issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        # Fetched on first use and cached for 10 minutes; a token with an unknown key id refetches.
        self._keys = keys or jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", lifespan=600, timeout=10)

    async def verify(self, token: str) -> AuthUser:
        """The token's user; raises a `jwt` error if it is forged, expired or not for this project."""
        # A cache miss is a blocking HTTP call, so it runs off the event loop.
        key = await asyncify(self._keys.get_signing_key_from_jwt)(token)
        claims = jwt.decode(
            token,
            key.key,
            # Asymmetric only: the public key can check a token but never sign one.
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            issuer=self.issuer,
            options={"require": ["exp", "sub"]},
        )
        return AuthUser(id=claims["sub"], email=claims.get("email"))


# Read by hand below (auto_error=False), so a missing token gets our own message.
bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """A 401 that tells the client to send a bearer token."""
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def require_user(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)] = None
) -> AuthUser | None:
    """The signed-in user, or a 401. `None` in local mode (no Supabase URL set), where there is no login."""
    verifier: TokenVerifier | None = request.app.state.auth
    if verifier is None:
        return None
    if credentials is None:
        raise _unauthorized("Sign in to use agentspace.")
    try:
        return await verifier.verify(credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Your session expired. Sign in again.") from exc
    except jwt.PyJWKClientConnectionError as exc:
        # Our side can't reach Supabase: the token may be fine, so this isn't a 401.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Can't reach the sign-in service.") from exc
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
        raise _unauthorized("Your sign-in isn't valid. Sign in again.") from exc
