import json
import time
from typing import Any

from .telegram_utils import send_document_bytes


def build_jsonl_bytes(records: list[dict]) -> bytes:
    if not records:
        return b""

    lines = [json.dumps(record, ensure_ascii=False, default=str) for record in records]
    payload = "\n".join(lines) + "\n"
    return payload.encode("utf-8")


async def send_permanent_records_archive(
    bot: Any,
    user_id: int,
    archived_records: list[dict],
    *,
    logger=None,
) -> bool:
    if not archived_records:
        return False

    payload = build_jsonl_bytes(archived_records)
    if not payload:
        return False

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"permanent_records_archive_{user_id}_{timestamp}.jsonl"
    caption = "你的永久记忆已超过上限，最旧的记录已打包成JSONL文件发给你。服务器存不下了，请自行保存处理。"
    return await send_document_bytes(
        bot,
        user_id,
        payload,
        filename,
        caption=caption,
        logger=logger,
    )
