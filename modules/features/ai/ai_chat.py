"""Facade exports for AI chat features."""

from .clients import (
    create_azure_client,
    create_gemini_client,
    create_zhipu_client,
)
from .router import get_ai_response
from .tasks.translate import translate_text
from .tasks.vision import analyze_image

__all__ = [
    "create_azure_client",
    "create_gemini_client",
    "create_zhipu_client",
    "get_ai_response",
    "translate_text",
    "analyze_image",
]

