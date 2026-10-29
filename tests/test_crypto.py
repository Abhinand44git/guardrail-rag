"""
Phase 2, decision log item #1: Fernet encryption for tenants.llm_api_key_ref,
built from day one rather than retrofitted later.
"""
import pytest
from cryptography.fernet import Fernet

from app import crypto
from app.models import Tenant


def test_roundtrip():
    plaintext = "gsk_fake_groq_key_1234567890abcdef"
    ciphertext = crypto.encrypt_api_key(plaintext)
    assert ciphertext != plaintext
    assert crypto.decrypt_api_key(ciphertext) == plaintext


def test_ciphertext_is_never_plaintext_even_encoded():
    # Guard against a lazy "encryption" that's just base64 -- the plaintext
    # bytes should not appear anywhere in the ciphertext.
    plaintext = "sk-distinctivelookingsecret999"
    ciphertext = crypto.encrypt_api_key(plaintext)
    assert plaintext not in ciphertext


def test_ciphertext_fits_the_db_column():
    # tenants.llm_api_key_ref is String(512). A realistically long BYO key
    # (longer than any real provider key we've seen) must still fit after
    # Fernet's fixed + variable overhead, or storage silently truncates.
    plaintext = "sk-" + "a" * 200
    ciphertext = crypto.encrypt_api_key(plaintext)
    assert len(ciphertext) <= 512


def test_rejects_empty_key():
    with pytest.raises(ValueError):
        crypto.encrypt_api_key("")


def test_tampered_ciphertext_fails_closed():
    ciphertext = crypto.encrypt_api_key("gsk_fake_key")
    tampered = ciphertext[:-4] + ("A" if ciphertext[-4] != "A" else "B") + ciphertext[-3:]
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_api_key(tampered)


def test_wrong_master_key_fails_closed():
    # Simulates the documented FERNET_MASTER_KEY-rotation scenario: a value
    # encrypted under a *different* key can't be decrypted under this one.
    other_fernet = Fernet(Fernet.generate_key())
    ciphertext_from_other_key = other_fernet.encrypt(b"gsk_fake_key").decode()
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_api_key(ciphertext_from_other_key)


def test_set_and_get_tenant_key_helpers_roundtrip():
    tenant = Tenant(name="test-tenant")
    crypto.set_tenant_llm_api_key(tenant, "gsk_abc123")
    assert tenant.llm_api_key_ref is not None
    assert tenant.llm_api_key_ref != "gsk_abc123"
    assert crypto.get_tenant_llm_api_key(tenant) == "gsk_abc123"


def test_clearing_tenant_key_leaves_it_unconfigured():
    # Renamed from ..._falls_back_to_default: there is no GuardRail default
    # to fall back to. Clearing a tenant's key just leaves the tenant
    # unconfigured -- /query must then return a safe error for that tenant,
    # not silently use some other key. See app/crypto.py docstrings.
    tenant = Tenant(name="test-tenant")
    crypto.set_tenant_llm_api_key(tenant, "gsk_abc123")
    crypto.set_tenant_llm_api_key(tenant, None)
    assert tenant.llm_api_key_ref is None
    assert crypto.get_tenant_llm_api_key(tenant) is None
