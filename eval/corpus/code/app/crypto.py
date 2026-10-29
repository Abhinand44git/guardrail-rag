"""
Fernet symmetric encryption for tenants.llm_api_key_ref.

Two-tier secret model (see decision log):
  1. Static app secrets (DB password, JWT signing key, FERNET_MASTER_KEY
     itself) -> hosting platform env vars, never in code/Git. GuardRail has
     no generation-provider key of its own in this tier -- it owns no model.
  2. Per-tenant BYO API keys -> dynamic per-row data, so they're encrypted
     here with Fernet before ever being written to the DB. Decrypted only
     in memory, only for the one outbound call to the tenant's chosen LLM
     provider. Never logged.

Key rotation is explicitly out of scope (see decision log): if
FERNET_MASTER_KEY is ever rotated, every existing llm_api_key_ref becomes
undecryptable. That's a documented, accepted limitation, not a bug --
proper handling needs key versioning per row and a re-encryption migration,
and there's no supporting incident-response infrastructure (no audit log,
no leak detection) that would make that machinery useful yet.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

if TYPE_CHECKING:
    from app.models import Tenant

# Built once at import time. settings.fernet_master_key is validated as a
# well-formed Fernet key at Settings load time (see app/config.py) -- if it
# weren't, this would raise here, at startup, not on the first encrypt call.
_fernet = Fernet(settings.fernet_master_key.encode())


class DecryptionError(Exception):
    """Raised when llm_api_key_ref can't be decrypted with the current
    FERNET_MASTER_KEY: wrong/rotated key, corrupted value, or a tampered
    ciphertext. Callers should treat this as "tenant has no usable BYO key"
    and route to the same safe "not configured yet" error /query already
    returns for a tenant with no key at all (Design doc, Diagram C, step 4)
    -- not crash the request.

    Corrected here: this docstring previously said to "fail over to the
    default provider key," a leftover from the pre-v2.0 architecture where
    GuardRail shipped its own default model. That fallback doesn't exist in
    the corrected design -- there is no GuardRail-owned key to fail over
    to. The decision log and README both already claimed this file's
    docstrings were fixed as part of migration 0004's cleanup; this one
    line was missed at the time and is fixed now, found by re-reading this
    file while verifying those claims for Phase 3.5, not by a failing
    test -- crypto.py has no behavior change, only this comment."""


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt a client-provided API key for storage in llm_api_key_ref."""
    if not plaintext:
        raise ValueError("cannot encrypt an empty API key")
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt a stored llm_api_key_ref value. Never log the return value."""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise DecryptionError(
            "llm_api_key_ref could not be decrypted with the current FERNET_MASTER_KEY"
        ) from e


def set_tenant_llm_api_key(tenant: "Tenant", plaintext_key: str | None) -> None:
    """
    Encrypt and assign a client's BYO API key onto a Tenant ORM object.
    Pass None to clear it -- there is no GuardRail default to fall back to,
    so clearing the key leaves the tenant unconfigured: /query will return
    a safe "ask your Admin" error for any user without a personal override
    (see Design doc, Diagram C, step 4). Caller is responsible for
    flush/commit -- this only touches the in-memory object.
    """
    tenant.llm_api_key_ref = encrypt_api_key(plaintext_key) if plaintext_key else None


def get_tenant_llm_api_key(tenant: "Tenant") -> str | None:
    """
    Decrypt a tenant's BYO API key for exactly one outbound generation call.
    Returns None if the tenant has no company-default key configured -- the
    caller must treat that as "this tenant isn't configured yet" (Design
    Diagram C, step 4), not substitute any other key. There is no fallback
    key anywhere in this system. Never log the return value.
    """
    if not tenant.llm_api_key_ref:
        return None
    return decrypt_api_key(tenant.llm_api_key_ref)
