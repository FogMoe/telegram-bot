import asyncio

_CONVERSATION_LOCKS: dict[int, asyncio.Lock] = {}


def get_conversation_lock(conversation_id: int) -> asyncio.Lock:
    lock = _CONVERSATION_LOCKS.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _CONVERSATION_LOCKS[conversation_id] = lock
    return lock
