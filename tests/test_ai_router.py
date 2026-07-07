import asyncio

import pytest

from features.ai import router


@pytest.fixture(autouse=True)
def clear_provider_circuit_state():
    router._provider_failure_streaks.clear()
    router._provider_circuit_open_until.clear()
    yield
    router._provider_failure_streaks.clear()
    router._provider_circuit_open_until.clear()


def test_get_ai_response_retries_image_messages_as_text(monkeypatch):
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        }
    ]
    calls = []

    async def fake_try_ai_services(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
        text_fallback_messages=None,
    ):
        calls.append(messages)
        if len(calls) == 1:
            return None, RuntimeError("provider failed")
        return ("text fallback response", []), None

    monkeypatch.setattr(router, "_try_ai_services", fake_try_ai_services)

    response = asyncio.run(router.get_ai_response(image_messages, user_id=123))

    assert response == ("text fallback response", [])
    assert calls == [
        image_messages,
        [{"role": "user", "content": "describe this image"}],
    ]


def test_visible_content_was_sent_counts_media_messages():
    class _VisibleHandler:
        sent_count = 0
        sent_contents = []
        sent_messages = [object()]

    assert router._visible_content_was_sent(_VisibleHandler()) is True


def test_text_only_chat_provider_uses_vision_text_fallback_messages(monkeypatch):
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "runtime message without description"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        }
    ]
    text_fallback_messages = [
        {
            "role": "user",
            "content": (
                "<metadata><media type=\"photo\">"
                "<description>a cat on a desk</description>"
                "</media></metadata><message>[photo]</message>"
            ),
        }
    ]
    calls = []

    def fake_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append(messages)
        return "ok", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["siliconflow"])
    monkeypatch.setattr(
        router,
        "AI_SERVICE_MAP",
        {"siliconflow": fake_service},
    )
    monkeypatch.setattr(
        router,
        "chat_service_supports_vision",
        lambda service_name: False,
    )
    monkeypatch.setattr(
        router,
        "chat_model_for_service",
        lambda service_name: "deepseek-ai/DeepSeek-V4-Flash",
    )

    response = asyncio.run(
        router.get_ai_response(
            image_messages,
            user_id=123,
            text_fallback_messages=text_fallback_messages,
        )
    )

    assert response == ("ok", [])
    assert calls == [text_fallback_messages]


def test_vision_capable_chat_provider_keeps_multimodal_messages(monkeypatch):
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        }
    ]
    calls = []

    def fake_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append(messages)
        return "ok", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["openai"])
    monkeypatch.setattr(router, "AI_SERVICE_MAP", {"openai": fake_service})
    monkeypatch.setattr(router, "chat_service_supports_vision", lambda service_name: True)

    response = asyncio.run(router.get_ai_response(image_messages, user_id=123))

    assert response == ("ok", [])
    assert calls == [image_messages]


def test_provider_circuit_opens_after_three_consecutive_failures_in_window():
    router._record_provider_failure("gemini", now=100.0)
    router._record_provider_failure("gemini", now=200.0)

    assert router._provider_circuit_is_open("gemini", now=250.0) is False

    router._record_provider_failure("gemini", now=300.0)

    assert router._provider_circuit_is_open("gemini", now=300.0) is True
    assert router._provider_circuit_is_open(
        "gemini",
        now=300.0 + router.AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS - 1,
    ) is True
    assert router._provider_circuit_is_open(
        "gemini",
        now=300.0 + router.AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    ) is False


def test_provider_circuit_does_not_count_failures_outside_window():
    router._record_provider_failure("gemini", now=0.0)
    router._record_provider_failure(
        "gemini",
        now=router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS + 1,
    )
    router._record_provider_failure(
        "gemini",
        now=router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS + 2,
    )

    assert router._provider_circuit_is_open(
        "gemini",
        now=router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS + 2,
    ) is False


def test_provider_success_resets_consecutive_failure_streak():
    router._record_provider_failure("gemini", now=100.0)
    router._record_provider_failure("gemini", now=200.0)
    router._record_provider_success("gemini")
    router._record_provider_failure("gemini", now=300.0)

    assert router._provider_circuit_is_open("gemini", now=300.0) is False
    assert router._provider_failure_streaks["gemini"] == [300.0]


def test_open_provider_circuit_skips_to_next_service(monkeypatch):
    calls = []

    def failing_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append("gemini")
        raise AssertionError("open circuit provider should be skipped")

    def fallback_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append("siliconflow")
        return "ok", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["gemini", "siliconflow"])
    monkeypatch.setattr(
        router,
        "AI_SERVICE_MAP",
        {
            "gemini": failing_service,
            "siliconflow": fallback_service,
        },
    )
    monkeypatch.setattr(router, "_provider_circuit_is_open", lambda service_name: service_name == "gemini")

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert response == ("ok", [])
    assert calls == ["siliconflow"]
