"""同步 HTTP 会话的登记，让关停时能统一关闭。

同步工具（requests 实现的 HTTP 工具）按线程各持一个 `requests.Session`，线程池里的线程退出后
没有人会关闭它们。工具创建会话时调用 `track_session`，进程停止时 `close_tracked_sessions()`
统一关闭，避免连接泄漏。弱引用登记：会话先被回收也不要紧。
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SESSIONS: weakref.WeakSet[Any] = weakref.WeakSet()


def track_session[S](session: S) -> S:
    """登记一个带 `close()` 的会话并原样返回，便于 `session = track_session(requests.Session())`。"""
    with _LOCK:
        _SESSIONS.add(session)
    return session


def tracked_count() -> int:
    with _LOCK:
        return len(_SESSIONS)


def close_tracked_sessions() -> int:
    """关闭所有登记过的会话，返回关闭的个数。单个会话关闭失败只记录，不影响其余。"""
    with _LOCK:
        sessions = list(_SESSIONS)
        _SESSIONS.clear()
    closed = 0
    for session in sessions:
        try:
            session.close()
            closed += 1
        except Exception:
            logger.warning("Failed to close an HTTP session", exc_info=True)
    return closed
