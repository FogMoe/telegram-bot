"""模拟旧部署里可能存在的数据，供迁移测试共用。"""

import json

from mysql_support import execute


def conversation_json(label: str) -> str:
    """应用能读懂的一条最小对话记录。"""
    return json.dumps([{"role": "user", "content": label}])


def first_message_content(messages_json: str) -> str:
    return json.loads(messages_json)[0]["content"]


def seed_duplicate_rows(url: str) -> None:
    """0016 schema 的库里可能出现的重复数据：chat_records.id 全是 0（非 strict 模式留下的），
    conversation_id / user_id 重复。

    - conversation 1：三行，最新（last_rotated_at 最大）的是 "rotated"
    - user 7：三行，保留 last_lottery_date 最大的一行（2024-03-01）
    """
    insert = (
        "INSERT INTO chat_records (id, conversation_id, messages, `timestamp`, last_rotated_at) "
        "VALUES (0, %s, %s, %s, %s)"
    )
    execute(
        url,
        (insert, (1, conversation_json("oldest"), "2024-01-01 00:00:00", None)),
        (insert, (1, conversation_json("newer"), "2024-01-02 00:00:00", None)),
        (insert, (1, conversation_json("rotated"), "2024-01-03 00:00:00", "2024-02-01 00:00:00")),
        (insert, (2, conversation_json("solo"), "2024-01-01 00:00:00", None)),
        "INSERT INTO user_lottery VALUES "
        "(7, '2024-01-01 00:00:00'), (7, '2024-03-01 00:00:00'), (7, NULL), "
        "(8, NULL), (9, '2024-01-01 00:00:00')",
    )
