import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

ISSUER = "https://kubetoole-il8h58.ch1.zitadel.cloud"
PROJECT_ID = "400874651212903737"
jwks = PyJWKClient(f"{ISSUER}/oauth/v2/keys", timeout=5)
bearer = HTTPBearer(auto_error=False)


def verify_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        key = jwks.get_signing_key_from_jwt(credentials.credentials)
        payload = jwt.decode(credentials.credentials, key.key, algorithms=["RS256"],
                             issuer=ISSUER, audience=PROJECT_ID,
                             options={"require": ["exp", "iss", "sub", "aud"]})
        if not isinstance(payload["sub"], str) or not payload["sub"]:
            raise jwt.InvalidTokenError()
        return payload["sub"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired authentication token",
                            headers={"WWW-Authenticate": "Bearer"}) from None
