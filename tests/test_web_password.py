import asyncio
import hashlib

from features.economy.operations import web_password


def test_hash_password_uses_argon2id_and_verifies():
    password_hash = web_password.hash_password("abc12345")

    assert password_hash.startswith("$argon2id$")
    assert len(password_hash) <= 255
    assert web_password.verify_password("abc12345", password_hash) is True
    assert web_password.verify_password("wrong123", password_hash) is False
    assert web_password.password_needs_rehash(password_hash) is False


def test_hash_password_uses_a_fresh_salt_per_call():
    first = web_password.hash_password("abc12345")
    second = web_password.hash_password("abc12345")

    assert first != second


def test_verify_password_rejects_legacy_unsalted_sha256():
    legacy_hash = hashlib.sha256(b"abc12345").hexdigest()

    assert web_password.verify_password("abc12345", legacy_hash) is False
    assert web_password.verify_password("abc12345", "") is False


def test_set_web_password_stores_argon2id_hash(monkeypatch):
    stored = {}

    async def fake_get(user_id):
        return None

    async def fake_set(user_id, password_hash):
        stored[user_id] = password_hash
        return True

    monkeypatch.setattr(web_password, "get_user_web_password", fake_get)
    monkeypatch.setattr(web_password, "set_user_web_password", fake_set)

    result = asyncio.run(web_password.process_set_web_password(7, "abc12345"))

    assert result.status is web_password.SetPasswordStatus.SAVED
    assert result.is_update is False
    assert stored[7].startswith("$argon2id$")
    assert "abc12345" not in stored[7]
