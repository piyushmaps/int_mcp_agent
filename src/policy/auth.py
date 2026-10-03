"""Local synthetic identity or verified JWT identity; user-supplied actor IDs are never trusted."""
import asyncio
import hashlib
import hmac

import jwt
from fastapi import HTTPException, Request

from src.contracts import Actor


class Authenticator:
    def __init__(self, settings):
        self.settings = settings
        self.jwks = jwt.PyJWKClient(settings.jwt_jwks_url, timeout=5) if settings.auth_mode == "jwt" else None

    async def __call__(self, request: Request) -> Actor:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})
        token = authorization[7:]
        if self.settings.auth_mode == "demo":
            host = request.client.host if request.client else ""
            if host not in {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}:
                raise HTTPException(403, "demo_is_local_only")
            if not hmac.compare_digest(token, self.settings.demo_token.get_secret_value()):
                raise HTTPException(401, "invalid_credentials")
            return Actor(id="local-demo", admin=True)
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.settings.jwt_audience,
                                issuer=self.settings.jwt_issuer, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            groups = claims.get("groups", [])
            if not isinstance(groups, list) or self.settings.jwt_required_group not in groups:
                raise HTTPException(403, "application_access_denied")
            actor = hashlib.sha256((claims["iss"] + "|" + claims["sub"]).encode()).hexdigest()
            return Actor(id=actor, admin=self.settings.jwt_admin_group in groups)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "invalid_credentials") from None

