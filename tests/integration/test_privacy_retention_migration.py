"""0018_privacy_retention：旧的 web 密码哈希与群聊历史里的敏感命令参数。"""

import base64

import pytest
from mysql_support import current_versions, execute, fetch, head_revision, upgrade

REV_0017 = "0017_schema_contracts"
HEAD = head_revision()

ARGON2_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"
SHA256_HEX = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"

# 用户 id -> password 列；只有 argon2id 开头的会在迁移后保留
WEB_PASSWORDS = {
    1: SHA256_HEX,
    2: ARGON2_HASH,
    3: ARGON2_HASH.upper(),
    4: "$argon2i$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaGhhc2hoYXNoaGFzaA",
    5: "",
}

# 原文 -> 迁移后（文本消息）
TEXT_MESSAGES = {
    "/charge ABC-123-SECRET": "/charge [redacted]",
    "/Charge@FogMoeBot ABC-123-SECRET": "/Charge@FogMoeBot [redacted]",
    "/WEBPASSWORD hunter2\nsecond line of secret": "/WEBPASSWORD [redacted]",
    "/webpassword    spaced   out   args": "/webpassword [redacted]",
    "/webpassword\tTabSeparated1": "/webpassword [redacted]",
    # 不带参数 / 已经脱敏 / 不是消息开头的命令 / 别的命令：保持原样
    "/charge": "/charge",
    "/charge@FogMoeBot": "/charge@FogMoeBot",
    "/charge [redacted]": "/charge [redacted]",
    "please /charge ABC-123": "please /charge ABC-123",
    "/chargeback ABC-123": "/chargeback ABC-123",
    "/webpassword2 abc123": "/webpassword2 abc123",
    "/help charge": "/help charge",
    "an ordinary message about passwords": "an ordinary message about passwords",
}

LONG_CAPTION = "a long caption " * 20


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _readable(value: str) -> str:
    """应用读取非文本消息时的解码规则：解不开就当作明文。"""
    try:
        return _decode(value)
    except ValueError:
        return value


# 非文本消息：应用把说明文字做 base64 后存进 content（message_id 从 1001 开始）
NON_TEXT_MESSAGES = [
    ("photo", _b64("/charge CAPTION-SECRET")),
    ("document", _b64("/Webpassword@FogMoeBot doc secret")),
    ("photo", _b64("nice picture")),
    ("sticker", _b64("😀")),
    ("photo", _b64(LONG_CAPTION)),
    ("photo", _b64("/charge " + LONG_CAPTION)),
    # 遗留的明文行：解不开 base64 时应用会把它当明文读
    ("other", "/webpassword plain-text-left-in-a-non-text-row"),
]


@pytest.fixture
def seeded_database(migrated_database):
    """迁移到 0017 之后写入旧数据，再让 0018 处理它们。"""
    execute(
        migrated_database,
        *[
            ("INSERT INTO `user` (id, name) VALUES (%s, %s)", (user_id, f"user{user_id}"))
            for user_id in WEB_PASSWORDS
        ],
        *[
            ("INSERT INTO web_password (user_id, password) VALUES (%s, %s)", (user_id, value))
            for user_id, value in WEB_PASSWORDS.items()
        ],
        *[
            (
                "INSERT INTO chat_records_group (group_id, message_id, user_id, message_type, content) "
                "VALUES (-100, %s, 1, 'text', %s)",
                (index, text),
            )
            for index, text in enumerate(TEXT_MESSAGES, start=1)
        ],
        *[
            (
                "INSERT INTO chat_records_group (group_id, message_id, user_id, message_type, content) "
                "VALUES (-100, %s, 1, %s, %s)",
                (1000 + index, message_type, content),
            )
            for index, (message_type, content) in enumerate(NON_TEXT_MESSAGES, start=1)
        ],
        ("UPDATE alembic_version SET version_num = %s", (REV_0017,)),
    )
    return migrated_database


def _group_content(url: str) -> dict[int, str]:
    rows = fetch(url, "SELECT message_id, content FROM chat_records_group ORDER BY message_id")
    return {row["message_id"]: row["content"] for row in rows}


def test_legacy_web_password_hashes_are_removed_and_argon2id_is_kept(seeded_database):
    upgrade(seeded_database)

    remaining = fetch(seeded_database, "SELECT user_id, password FROM web_password ORDER BY user_id")

    assert current_versions(seeded_database) == [HEAD]
    assert remaining == [{"user_id": 2, "password": ARGON2_HASH}]


def test_sensitive_command_arguments_are_redacted_in_group_text_messages(seeded_database):
    upgrade(seeded_database)

    stored = _group_content(seeded_database)

    for index, (original, expected) in enumerate(TEXT_MESSAGES.items(), start=1):
        assert stored[index] == expected, original


def test_sensitive_command_arguments_are_redacted_in_encoded_group_messages(seeded_database):
    before = _group_content(seeded_database)

    upgrade(seeded_database)

    stored = _group_content(seeded_database)
    assert _decode(stored[1001]) == "/charge [redacted]"
    assert _decode(stored[1002]) == "/Webpassword@FogMoeBot [redacted]"
    assert _decode(stored[1006]) == "/charge [redacted]"
    # 编码后的内容和应用写入时一样不含换行，应用的解码函数能读回来。
    assert all("\n" not in stored[message_id] for message_id in (1001, 1002, 1006))
    assert stored[1007] == "/webpassword [redacted]"
    # 普通消息一字节都不变，包括需要折行的长文本。
    for message_id in (1003, 1004, 1005):
        assert stored[message_id] == before[message_id]
    assert _decode(stored[1005]) == LONG_CAPTION


def test_secrets_do_not_survive_anywhere_in_group_history(seeded_database):
    upgrade(seeded_database)

    stored = _group_content(seeded_database)
    readable = [_readable(value) for value in stored.values()]

    secrets = (
        "ABC-123-SECRET",
        "hunter2",
        "second line of secret",
        "spaced",
        "TabSeparated1",
        "CAPTION-SECRET",
        "doc secret",
        "plain-text-left",
    )
    for secret in secrets:
        assert not any(secret in value for value in readable), secret


def test_running_the_migration_again_changes_nothing(seeded_database):
    upgrade(seeded_database)
    web_passwords = fetch(seeded_database, "SELECT user_id, password FROM web_password")
    group_content = _group_content(seeded_database)

    execute(seeded_database, ("UPDATE alembic_version SET version_num = %s", (REV_0017,)))
    upgrade(seeded_database)

    assert current_versions(seeded_database) == [HEAD]
    assert fetch(seeded_database, "SELECT user_id, password FROM web_password") == web_passwords
    assert _group_content(seeded_database) == group_content


def test_migration_works_when_there_is_nothing_to_clean(migrated_database):
    execute(migrated_database, ("UPDATE alembic_version SET version_num = %s", (REV_0017,)))

    upgrade(migrated_database)

    assert current_versions(migrated_database) == [HEAD]
