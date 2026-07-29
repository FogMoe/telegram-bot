import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from features.account import fogmoe_account


ISSUER = "https://issuer.example/auth/v1"
CLIENT_ID = "telegram-client"
AUDIENCE = "telegram-bot"
SUBJECT = "11111111-2222-3333-4444-555555555555"


def _configure(monkeypatch):
    monkeypatch.setattr(fogmoe_account.config, "FOGMOE_OAUTH_ENABLED", True)
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_DISCOVERY_URL",
        "https://issuer.example/auth/v1/.well-known/openid-configuration",
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_EXPECTED_ISSUER",
        ISSUER,
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_CLIENT_ID",
        CLIENT_ID,
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_CLIENT_SECRET",
        "test-secret",
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_AUDIENCE",
        AUDIENCE,
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_REDIRECT_URI",
        "https://bot.example/oauth/fogmoe/callback",
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_SCOPES",
        "openid email profile",
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_ALLOWED_ALGORITHMS",
        "ES256",
    )


def _metadata():
    return fogmoe_account.OAuthMetadata(
        issuer=ISSUER,
        authorization_endpoint="https://issuer.example/oauth/authorize",
        token_endpoint="https://issuer.example/oauth/token",
        jwks_uri="https://issuer.example/.well-known/jwks.json",
    )


def _transaction(
    *,
    nonce="test-nonce",
    action="bind",
    expected_subject=None,
):
    return fogmoe_account.OAuthTransaction(
        state_hash="a" * 64,
        telegram_user_id=123,
        chat_id=123,
        code_verifier="v" * 64,
        nonce=nonce,
        redirect_uri="https://bot.example/oauth/fogmoe/callback",
        requested_scopes=("openid", "email", "profile"),
        action=action,
        expected_subject=expected_subject,
    )


def _token_pair(*, nonce="test-nonce", access_subject=SUBJECT):
    key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = jwt.algorithms.ECAlgorithm.to_jwk(
        key.public_key(),
        as_dict=True,
    )
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "ES256"})
    now = datetime.now(timezone.utc)
    common = {
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    id_token = jwt.encode(
        {
            **common,
            "sub": SUBJECT,
            "aud": CLIENT_ID,
            "nonce": nonce,
        },
        key,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )
    access_token = jwt.encode(
        {
            **common,
            "sub": access_subject,
            "aud": AUDIENCE,
            "client_id": CLIENT_ID,
            "scope": "openid email profile",
            "preferred_username": "fog-user",
        },
        key,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )
    return (
        {
            "id_token": id_token,
            "access_token": access_token,
            "scope": "openid email profile",
        },
        {"keys": [public_jwk]},
    )


def test_configuration_requires_https_callback_and_identity_scopes(monkeypatch):
    _configure(monkeypatch)
    fogmoe_account.validate_configuration()

    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_REDIRECT_URI",
        "http://bot.example/oauth/fogmoe/callback",
    )
    with pytest.raises(fogmoe_account.OAuthConfigurationError):
        fogmoe_account.validate_configuration()

    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_REDIRECT_URI",
        "https://bot.example/oauth/fogmoe/callback",
    )
    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_SCOPES",
        "openid profile",
    )
    with pytest.raises(
        fogmoe_account.OAuthConfigurationError,
        match="openid email profile",
    ):
        fogmoe_account.validate_configuration()

    monkeypatch.setattr(
        fogmoe_account.config,
        "FOGMOE_OAUTH_SCOPES",
        "openid profile email",
    )
    with pytest.raises(
        fogmoe_account.OAuthConfigurationError,
        match="openid email profile",
    ):
        fogmoe_account.validate_configuration()


def test_pkce_challenge_uses_sha256_base64url_without_padding():
    challenge = fogmoe_account._pkce_challenge(
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )

    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorization_url_requests_email_and_username_scopes(monkeypatch):
    _configure(monkeypatch)
    stored_transactions = []

    async def fake_get_metadata():
        return _metadata()

    async def fake_store_transaction(**kwargs):
        stored_transactions.append(kwargs)

    monkeypatch.setattr(fogmoe_account, "get_metadata", fake_get_metadata)
    monkeypatch.setattr(
        fogmoe_account,
        "_store_transaction",
        fake_store_transaction,
    )

    authorization_url = asyncio.run(
        fogmoe_account._create_authorization_url(
            telegram_user_id=123,
            chat_id=123,
            action="bind",
        )
    )

    query = parse_qs(urlparse(authorization_url).query)
    assert query["scope"] == ["openid email profile"]
    assert stored_transactions[0]["requested_scopes"] == (
        "openid",
        "email",
        "profile",
    )


def test_token_pair_validates_signature_claims_and_subject(monkeypatch):
    _configure(monkeypatch)
    token_response, jwks = _token_pair()

    async def fake_get_jwks(metadata, *, force_refresh=False):
        return jwks

    monkeypatch.setattr(fogmoe_account, "_get_jwks", fake_get_jwks)
    identity = asyncio.run(
        fogmoe_account.verify_token_pair(
            token_response,
            _transaction(),
            _metadata(),
        )
    )

    assert identity.subject == str(UUID(SUBJECT))
    assert identity.username == "fog-user"


def test_token_pair_rejects_nonce_and_cross_token_subject_mismatches(monkeypatch):
    _configure(monkeypatch)
    token_response, jwks = _token_pair(nonce="wrong")

    async def fake_get_jwks(metadata, *, force_refresh=False):
        return jwks

    monkeypatch.setattr(fogmoe_account, "_get_jwks", fake_get_jwks)
    with pytest.raises(fogmoe_account.OAuthFlowError, match="nonce"):
        asyncio.run(
            fogmoe_account.verify_token_pair(
                token_response,
                _transaction(),
                _metadata(),
            )
        )

    token_response, jwks = _token_pair(
        access_subject="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(fogmoe_account.OAuthFlowError, match="subject"):
        asyncio.run(
            fogmoe_account.verify_token_pair(
                token_response,
                _transaction(),
                _metadata(),
            )
        )


class _FakeResult:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class _FakeConnection:
    def __init__(self):
        self.statements = []

    async def exec_driver_sql(self, sql, params=None):
        statement = " ".join(sql.split())
        self.statements.append((statement, params))
        return _FakeResult()


def test_first_binding_rewards_once_in_the_same_transaction(monkeypatch):
    connection = _FakeConnection()
    rows = iter([(123,), None, None, None])
    rewards = []

    @asynccontextmanager
    async def fake_transaction():
        yield connection

    async def fake_fetch_one(*args, **kwargs):
        return next(rows)

    async def fake_add_free_coins(user_id, coins, *, connection):
        rewards.append((user_id, coins, connection))
        return coins

    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "transaction",
        fake_transaction,
    )
    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "fetch_one",
        fake_fetch_one,
    )
    monkeypatch.setattr(
        fogmoe_account.process_user,
        "add_free_coins",
        fake_add_free_coins,
    )

    result = asyncio.run(
        fogmoe_account.bind_account(
            123,
            fogmoe_account.VerifiedIdentity(SUBJECT, "fog-user"),
        )
    )

    assert result.reward_granted is True
    assert rewards == [(123, 20, connection)]
    assert any(
        statement.startswith("INSERT INTO fogmoe_account_bindings")
        for statement, _ in connection.statements
    )
    assert any(
        statement.startswith("INSERT INTO fogmoe_binding_reward_claims")
        for statement, _ in connection.statements
    )


def test_existing_same_binding_is_verified_without_another_reward(monkeypatch):
    connection = _FakeConnection()
    rows = iter([(123,), (42, SUBJECT)])
    rewards = []

    @asynccontextmanager
    async def fake_transaction():
        yield connection

    async def fake_fetch_one(*args, **kwargs):
        return next(rows)

    async def fake_add_free_coins(*args, **kwargs):
        rewards.append((args, kwargs))
        return 20

    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "transaction",
        fake_transaction,
    )
    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "fetch_one",
        fake_fetch_one,
    )
    monkeypatch.setattr(
        fogmoe_account.process_user,
        "add_free_coins",
        fake_add_free_coins,
    )

    result = asyncio.run(
        fogmoe_account.bind_account(
            123,
            fogmoe_account.VerifiedIdentity(SUBJECT, "fog-user"),
        )
    )

    assert result.reward_granted is False
    assert rewards == []
    assert any(
        statement.startswith("UPDATE fogmoe_account_bindings")
        for statement, _ in connection.statements
    )


def test_rebinding_after_unbind_does_not_repeat_reward(monkeypatch):
    connection = _FakeConnection()
    rows = iter([(123,), None, None, (123,)])
    rewards = []

    @asynccontextmanager
    async def fake_transaction():
        yield connection

    async def fake_fetch_one(*args, **kwargs):
        return next(rows)

    async def fake_add_free_coins(*args, **kwargs):
        rewards.append((args, kwargs))
        return 20

    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "transaction",
        fake_transaction,
    )
    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "fetch_one",
        fake_fetch_one,
    )
    monkeypatch.setattr(
        fogmoe_account.process_user,
        "add_free_coins",
        fake_add_free_coins,
    )

    result = asyncio.run(
        fogmoe_account.bind_account(
            123,
            fogmoe_account.VerifiedIdentity(
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "new-account",
            ),
        )
    )

    assert result.reward_granted is False
    assert rewards == []
    assert any(
        statement.startswith("INSERT INTO fogmoe_account_bindings")
        for statement, _ in connection.statements
    )
    assert not any(
        statement.startswith("INSERT INTO fogmoe_binding_reward_claims")
        for statement, _ in connection.statements
    )


def test_unbind_requires_the_current_fogmoe_subject(monkeypatch):
    @asynccontextmanager
    async def unexpected_transaction():
        raise AssertionError("错误账号不应进入数据库事务")
        yield

    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "transaction",
        unexpected_transaction,
    )

    with pytest.raises(
        fogmoe_account.BindingConflictError,
        match="不是当前绑定",
    ):
        asyncio.run(
            fogmoe_account.unbind_account(
                123,
                fogmoe_account.VerifiedIdentity(
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "wrong-account",
                ),
                SUBJECT,
            )
        )


def test_verified_unbind_soft_deactivates_binding(monkeypatch):
    connection = _FakeConnection()

    @asynccontextmanager
    async def fake_transaction():
        yield connection

    async def fake_fetch_one(*args, **kwargs):
        return (42, SUBJECT, "fog-user")

    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "transaction",
        fake_transaction,
    )
    monkeypatch.setattr(
        fogmoe_account.mysql_connection,
        "fetch_one",
        fake_fetch_one,
    )

    username = asyncio.run(
        fogmoe_account.unbind_account(
            123,
            fogmoe_account.VerifiedIdentity(SUBJECT, "fog-user"),
            SUBJECT,
        )
    )

    assert username == "fog-user"
    assert any(
        "SET unbound_at = UTC_TIMESTAMP(6)" in statement
        for statement, _ in connection.statements
    )


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _FakeCallbackQuery:
    def __init__(self):
        self.answers = 0
        self.edits = []

    async def answer(self):
        self.answers += 1

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def test_existing_binding_shows_unbind_as_fogmoe_button(monkeypatch):
    _configure(monkeypatch)
    message = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(type="private", id=123),
        effective_user=SimpleNamespace(id=123),
        message=message,
    )

    async def fake_user_exists(user_id):
        return user_id == 123

    async def fake_get_binding(user_id):
        assert user_id == 123
        return {
            "subject": SUBJECT,
            "username": "fog-user",
        }

    monkeypatch.setattr(
        fogmoe_account.process_user,
        "async_user_exists",
        fake_user_exists,
    )
    monkeypatch.setattr(fogmoe_account, "get_binding", fake_get_binding)

    asyncio.run(
        fogmoe_account.fogmoe_account_command.__wrapped__(
            update,
            SimpleNamespace(),
        )
    )

    assert len(message.replies) == 1
    text, kwargs = message.replies[0]
    assert "已绑定 FOGMOE Account：fog-user" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "解绑或更换账号"
    assert button.callback_data == fogmoe_account.UNBIND_CALLBACK_DATA


def test_unbind_button_creates_current_account_oauth_request(monkeypatch):
    _configure(monkeypatch)
    query = _FakeCallbackQuery()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(type="private", id=123),
        effective_user=SimpleNamespace(id=123),
        callback_query=query,
    )
    authorization_requests = []

    async def fake_user_exists(user_id):
        return user_id == 123

    async def fake_get_binding(user_id):
        assert user_id == 123
        return {
            "subject": SUBJECT,
            "username": "fog-user",
        }

    async def fake_create_authorization_url(**kwargs):
        authorization_requests.append(kwargs)
        return "https://issuer.example/oauth/authorize?state=test"

    monkeypatch.setattr(
        fogmoe_account.process_user,
        "async_user_exists",
        fake_user_exists,
    )
    monkeypatch.setattr(fogmoe_account, "get_binding", fake_get_binding)
    monkeypatch.setattr(
        fogmoe_account,
        "_create_authorization_url",
        fake_create_authorization_url,
    )

    asyncio.run(
        fogmoe_account.fogmoe_account_unbind_callback(
            update,
            SimpleNamespace(),
        )
    )

    assert query.answers == 1
    assert authorization_requests == [{
        "telegram_user_id": 123,
        "chat_id": 123,
        "action": "unbind",
        "expected_subject": SUBJECT,
    }]
    assert len(query.edits) == 1
    text, kwargs = query.edits[0]
    assert "请使用当前已绑定的 FOGMOE Account 再次登录确认" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "验证并解绑 FOGMOE Account"
    assert button.url.startswith("https://issuer.example/oauth/authorize")
