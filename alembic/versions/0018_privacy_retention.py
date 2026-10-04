"""Remove sensitive data that was retained before redaction was applied everywhere.

- web_password：Web 登录系统已废弃，旧的无盐 SHA-256 哈希没有任何消费者了，
  删除所有不是 `$argon2id$` 开头的行；用户之后用 /webpassword 重新设置即可。
- chat_records_group：历史上群聊里的 `/charge <卡密>`、`/webpassword <密码>`（含
  `/charge@BotName ...`，命令名大小写不敏感）以明文写入 content。把参数替换成 `[redacted]`，
  命令本身保留。文本消息存明文；其他类型存 base64（见 core/group_chat_history.py），
  所以对非文本行先解码、脱敏、再编码回去。

两步都是幂等的，重复执行不会再改动任何数据。
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_privacy_retention"
down_revision = "0017_schema_contracts"
branch_labels = None
depends_on = None

# 命令必须出现在消息开头，之后至少跟一个非空白字符才算带参数
# （与 core.telegram_history 的 `text.split(maxsplit=1)` 判定一致）。
# 第 1 组保留原样（含 @BotName 和原始大小写），参数整体替换。
SENSITIVE_COMMAND_PATTERN = (
    "^(/(?:charge|webpassword)(?:@[^[:space:]]*)?)[[:space:]]+[^[:space:]].*$"
)
REDACTED_REPLACEMENT = "$1 [redacted]"
# i：大小写不敏感；n：`.` 可以匹配换行（参数可能跨行）。
MATCH_FLAGS = "in"


def _execute(sql: str) -> None:
    op.execute(
        sa.text(sql).bindparams(
            pattern=SENSITIVE_COMMAND_PATTERN,
            replacement=REDACTED_REPLACEMENT,
            flags=MATCH_FLAGS,
        )
    )


def upgrade() -> None:
    # 按字节比较，保证 `$ARGON2ID$` 之类的变体也会被清掉。
    op.execute(
        "DELETE FROM `web_password` WHERE `password` IS NULL "
        "OR CAST(LEFT(`password`, 10) AS BINARY) <> CAST('$argon2id$' AS BINARY)"
    )

    # 文本消息，以及非文本类型里直接存了明文的遗留行（base64 字母表不含空白，不会误匹配）。
    _execute(
        "UPDATE `chat_records_group` "
        "SET `content` = REGEXP_REPLACE(`content`, :pattern, :replacement, 1, 0, :flags) "
        "WHERE REGEXP_LIKE(`content`, :pattern, :flags)"
    )
    # 非文本类型（照片说明等）：解 base64 -> 脱敏 -> 编码回去，并去掉 TO_BASE64 插入的换行。
    _execute(
        "UPDATE `chat_records_group` "
        "SET `content` = REPLACE(TO_BASE64(REGEXP_REPLACE("
        "CONVERT(FROM_BASE64(`content`) USING utf8mb4), "
        ":pattern, :replacement, 1, 0, :flags)), CHAR(10), '') "
        "WHERE `message_type` <> 'text' AND `content` IS NOT NULL AND `content` <> '' "
        "AND FROM_BASE64(`content`) IS NOT NULL "
        "AND REGEXP_LIKE(CONVERT(FROM_BASE64(`content`) USING utf8mb4), :pattern, :flags)"
    )


def downgrade() -> None:
    # 被删除的哈希和被脱敏的参数无法恢复，降级什么都不做。
    pass
