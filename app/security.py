import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Excludes visually ambiguous characters (0/O, 1/l/I) since this password is
# read off an email by a human, not copy-pasted from a password manager.
_TEMP_PASSWORD_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits + "!@#$%^&*"
    if c not in "0O1lI"
)


def generate_temp_password(length: int = 16) -> str:
    """
    Cryptographically random one-time password for a newly created Admin
    (decision log item #4). Uses `secrets`, not `random` -- this password
    grants Admin-level access to a tenant, so it needs to be unguessable,
    not just look random. Never logged, never returned in an API response
    body -- delivered by email only (see app/email.py) and immediately
    hashed with bcrypt before the plaintext goes out of scope.
    """
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _encode(claims: dict) -> str:
    to_encode = claims.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    to_encode["exp"] = expire
    # jti: unique per minted token (not per user/session) -- this is what
    # token_blacklist.token_jti references (app/models.py, migration 0012).
    # Added now, not at Phase 2 where it should have been: the "token
    # blacklist" item in Phase 2's own build-phase description was never
    # actually built, found during a later audit. Every token minted before
    # this change has no jti; app/deps.py's blacklist check treats a
    # missing jti as "can't be individually blacklisted" rather than
    # erroring, so this is backward compatible with any such token still
    # in flight.
    to_encode["jti"] = str(uuid.uuid4())
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_operator_token(operator_id: uuid.UUID) -> str:
    """
    Operator plane token. Deliberately carries NO tenant_id claim —
    this is what makes operators structurally unable to reach tenant-scoped
    routes: they fail JWT claim validation before any role gate is checked.
    """
    return _encode({"sub": str(operator_id), "plane": "operator"})


def create_user_token(user_id: uuid.UUID, tenant_id: uuid.UUID, role: int) -> str:
    """Tenant-scoped user plane token. Always carries tenant_id + role."""
    return _encode({
        "sub": str(user_id),
        "plane": "user",
        "tenant_id": str(tenant_id),
        "role": role,
    })


class TokenError(Exception):
    pass


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise TokenError(str(e)) from e

    plane = payload.get("plane")
    if plane not in ("operator", "user"):
        raise TokenError("missing or invalid plane claim")

    if plane == "user" and ("tenant_id" not in payload or "role" not in payload):
        raise TokenError("user-plane token missing required claims")

    if plane == "operator" and "tenant_id" in payload:
        # Should never happen if tokens are only ever minted by create_operator_token,
        # but fail closed rather than silently trusting a malformed/tampered token.
        raise TokenError("operator-plane token must not carry tenant_id")

    return payload
