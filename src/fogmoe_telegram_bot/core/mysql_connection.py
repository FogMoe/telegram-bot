"""数据访问兼容层。

历史上全项目都从这里取 SQL 助手与聊天记录接口。实现已按领域拆到
`core.sql`、`core.chat_records`、`core.user_records`，这一层只做转发，
让调用方不必关心落点。新代码可以直接 import 对应领域模块。
"""

from .chat_records import (
    COIN_SERVICE_STATE_RESUMED,
    COIN_SERVICE_STATE_SUSPENDED,
    PERMANENT_RECORDS_KEEP,
    append_permanent_chat_record,
    archive_chat_and_start_new_session,
    async_get_chat_history,
    async_insert_chat_record,
    async_insert_chat_records,
    async_update_latest_history_state_summary,
    get_chat_history,
    insert_chat_record,
    insert_chat_records,
    prune_permanent_records,
)
from .sql import (
    connect,
    execute,
    fetch_all,
    fetch_one,
    run_sync,
    transaction,
)
from .user_records import async_check_user_exists, check_user_exists

__all__ = [
    "COIN_SERVICE_STATE_RESUMED",
    "COIN_SERVICE_STATE_SUSPENDED",
    "PERMANENT_RECORDS_KEEP",
    "append_permanent_chat_record",
    "archive_chat_and_start_new_session",
    "async_check_user_exists",
    "async_get_chat_history",
    "async_insert_chat_record",
    "async_insert_chat_records",
    "async_update_latest_history_state_summary",
    "check_user_exists",
    "connect",
    "execute",
    "fetch_all",
    "fetch_one",
    "get_chat_history",
    "insert_chat_record",
    "insert_chat_records",
    "prune_permanent_records",
    "run_sync",
    "transaction",
]
