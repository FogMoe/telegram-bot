import json


def build_jsonl_bytes(records: list[dict]) -> bytes:
    if not records:
        return b""

    lines = [json.dumps(record, ensure_ascii=False, default=str) for record in records]
    payload = "\n".join(lines) + "\n"
    return payload.encode("utf-8")
