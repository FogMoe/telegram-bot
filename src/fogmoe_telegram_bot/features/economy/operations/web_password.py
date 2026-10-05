"""网页密码的业务操作：格式校验、Argon2id 哈希与校验、设置与读取状态。不依赖 Telegram。

明文密码只在这里的哈希之前出现，存储与日志里只有哈希；哈希放到线程里算，避免阻塞事件循环。
读写出错时记脱敏日志并返回失败结果，由适配层给用户一个安全的错误描述。
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.core.redaction import log_exception

from ..repositories import web_passwords as web_password_repository
from ..repositories.web_passwords import WebPasswordRecord

logger = logging.getLogger(__name__)

# Argon2id，参数取库默认值（RFC 9106 低内存配置）；盐与参数都编码在 PHC 字符串里。
_PASSWORD_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    """用 Argon2id 对密码哈希，返回 PHC 字符串（约 100 字符，web_password.password 为 VARCHAR(255)）。"""
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码与 PHC 字符串是否匹配；格式不识别的哈希一律视为不匹配。"""
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """哈希参数低于当前配置时返回 True，供验证成功后的调用方重新哈希。"""
    return _PASSWORD_HASHER.check_needs_rehash(password_hash)


def validate_password(password: str) -> tuple[bool, str]:
    """验证密码格式，返回 (是否合法, 给用户看的说明)。"""
    # 密码长度6-20位，包含字母和数字
    if len(password) < 6 or len(password) > 20:
        return False, "密码长度必须在6-20位之间"

    if not re.match(r"^[a-zA-Z0-9]+$", password):
        return False, "密码只能包含字母和数字"

    # 必须包含至少一个字母和一个数字
    if not re.search(r"[a-zA-Z]", password) or not re.search(r"[0-9]", password):
        return False, "密码必须包含至少一个字母和一个数字"

    return True, "密码格式正确"


async def get_user_web_password(user_id: int) -> WebPasswordRecord | None:
    """读取用户的网页密码记录（只有哈希与时间）；没有记录或读取出错都返回 None。"""
    try:
        return await web_password_repository.get_web_password(user_id)
    except Exception as e:
        log_exception(logger, "获取用户Web密码信息失败", e)
        return None


async def set_user_web_password(user_id: int, password_hash: str) -> bool:
    """写入密码哈希（已有记录时更新）；写入出错返回 False。"""
    try:
        async with sql.transaction() as connection:
            await web_password_repository.save_web_password(connection, user_id, password_hash)
        return True
    except Exception as e:
        log_exception(logger, "设置用户Web密码失败", e)
        return False


class SetPasswordStatus(StrEnum):
    SAVED = "saved"
    INVALID = "invalid"  # 格式不合规，`message` 是原因
    FAILED = "failed"  # 写入出错


@dataclass(frozen=True, slots=True)
class SetPasswordResult:
    status: SetPasswordStatus
    message: str = ""  # INVALID：格式不合规的原因
    is_update: bool = False  # SAVED：是更新（之前已有密码）还是首次设置


async def process_set_web_password(user_id: int, password: str) -> SetPasswordResult:
    """校验格式、哈希并保存密码。"""
    is_valid, message = validate_password(password)
    if not is_valid:
        return SetPasswordResult(SetPasswordStatus.INVALID, message)

    # Argon2 占用 CPU 与内存，放到线程里避免阻塞事件循环
    password_hash = await asyncio.to_thread(hash_password, password)

    # 检查是否已有密码
    existing = await get_user_web_password(user_id)
    is_update = existing is not None

    if await set_user_web_password(user_id, password_hash):
        return SetPasswordResult(SetPasswordStatus.SAVED, is_update=is_update)
    return SetPasswordResult(SetPasswordStatus.FAILED)
