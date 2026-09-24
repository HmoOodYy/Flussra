"""
Password hashing and JWT token utilities.

No passlib wrapper — bcrypt is called directly to avoid a dependency on a
package in maintenance-only mode.
"""
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import HTTPException, status

from app.config import settings

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """
    Hash a plaintext password with bcrypt (cost factor 12).
    Returns a UTF-8 string safe to store in sec.Users.PasswordHash.
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """
    Verify a plaintext password against a stored bcrypt hash.
    Returns True if they match, False otherwise.
    Constant-time comparison — safe against timing attacks.
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"


def create_access_token(user_id: int, company_id: int) -> str:
    """
    Create a signed JWT access token containing user_id and company_id.
    Expiry is controlled by settings.ACCESS_TOKEN_EXPIRE_HOURS.
    """
    now = datetime.now(UTC)
    payload = {
        # RFC 7519 §4.1.2 requires "sub" to be a string; PyJWT 2.13+ enforces this.
        "sub": str(user_id),
        "cid": company_id,
        "iat": now,
        "exp": now + timedelta(hours=settings.ACCESS_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.

    Returns the payload dict on success.
    Raises HTTP 401 on expiry or any invalidity — never returns None.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
