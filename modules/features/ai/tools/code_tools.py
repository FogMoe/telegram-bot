import base64
import logging
import threading
from typing import Dict, Optional

import requests

from core import config

JUDGE0_API_URL = getattr(config, "JUDGE0_API_URL", "")
JUDGE0_API_KEY = getattr(config, "JUDGE0_API_KEY", "")
_SESSION_LOCAL = threading.local()


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _SESSION_LOCAL.session = session
    return session


def execute_python_code_tool(
    source_code: str,
    stdin: Optional[str] = None,
    **kwargs,
) -> dict:
    """Execute Python code remotely via Judge0."""
    if not (source_code and source_code.strip()):
        return {"error": "Source code is required"}

    base_url = (JUDGE0_API_URL or "").strip()
    if not base_url:
        return {"error": "Judge0 API URL is not configured"}

    request_url = f"{base_url.rstrip('/')}/submissions?base64_encoded=true&wait=true"

    headers = {"Content-Type": "application/json"}
    if JUDGE0_API_KEY:
        headers["X-Auth-Token"] = JUDGE0_API_KEY

    try:
        payload: Dict[str, object] = {
            "language_id": 71,
            "source_code": base64.b64encode(source_code.encode("utf-8")).decode("ascii"),
        }
        if stdin:
            payload["stdin"] = base64.b64encode(stdin.encode("utf-8")).decode("ascii")

        session = _get_session()
        response = session.post(request_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()
    except requests.HTTPError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        return {
            "error": "Submission failed",
            "status_code": getattr(exc.response, "status_code", None),
            "details": detail,
        }
    except requests.RequestException as exc:
        logging.exception("Request failed: %s", exc)
        return {"error": f"Failed to contact: {exc}"}

    def _decode_field(value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return ""
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except Exception:
            logging.warning("Failed to decode Judge0 field")
            return value

    status_info = result.get("status") or {}

    return {
        "token": result.get("token"),
        "status_id": status_info.get("id"),
        "status_description": status_info.get("description"),
        "stdout": _decode_field(result.get("stdout")),
        "stderr": _decode_field(result.get("stderr")),
        "compile_output": _decode_field(result.get("compile_output")),
        "message": result.get("message"),
        "time": result.get("time"),
        "memory": result.get("memory"),
    }


__all__ = ["execute_python_code_tool"]
