import pytest
from pydantic import ValidationError

from features.ai.tools import image_tools
from features.ai.tools.models import GenerateImageArgs, parameters_schema


def test_generate_image_timeout_schema_is_optional_and_bounded():
    schema = parameters_schema(GenerateImageArgs)

    timeout_schema = schema["properties"]["timeout_seconds"]

    assert "timeout_seconds" not in schema.get("required", [])
    assert timeout_schema["default"] == 45
    assert timeout_schema["minimum"] == 15
    assert timeout_schema["maximum"] == 75


@pytest.mark.parametrize("timeout_seconds", [14, 76])
def test_generate_image_timeout_validation_rejects_out_of_range_values(timeout_seconds):
    with pytest.raises(ValidationError):
        GenerateImageArgs.model_validate(
            {"prompt": "draw a cat", "timeout_seconds": timeout_seconds}
        )


def test_generate_image_tool_passes_model_timeout(monkeypatch):
    recorded_request = {}
    _prepare_successful_image_tool(monkeypatch, recorded_request)

    result = image_tools.generate_image_tool(
        prompt="draw a cat",
        timeout_seconds=45,
    )

    assert result["status"] == "generated"
    assert recorded_request["timeout"] == 45


def test_generate_image_tool_uses_config_default_timeout(monkeypatch):
    recorded_request = {}
    _prepare_successful_image_tool(monkeypatch, recorded_request)
    monkeypatch.setattr(image_tools.config, "IMAGE_GEN_TIMEOUT", 45)

    result = image_tools.generate_image_tool(prompt="draw a cat")

    assert result["status"] == "generated"
    assert recorded_request["timeout"] == 45


def test_generate_image_tool_clamps_direct_timeout(monkeypatch):
    recorded_request = {}
    _prepare_successful_image_tool(monkeypatch, recorded_request)

    result = image_tools.generate_image_tool(
        prompt="draw a cat",
        timeout_seconds=120,
    )

    assert result["status"] == "generated"
    assert recorded_request["timeout"] == 75


def _prepare_successful_image_tool(monkeypatch, recorded_request):
    monkeypatch.setattr(image_tools, "_cleanup_expired_generated_images", lambda: None)
    monkeypatch.setattr(image_tools.config, "IMAGE_GEN_API_URL", "https://example.test/generate")
    monkeypatch.setattr(image_tools.config, "IMAGE_GEN_API_TOKEN", "token")
    monkeypatch.setattr(image_tools, "_get_request_user_id", lambda: 123)
    monkeypatch.setattr(
        image_tools,
        "_reserve_image_generation",
        lambda user_id: (True, 1.0, None),
    )
    monkeypatch.setattr(
        image_tools,
        "_release_image_generation",
        lambda user_id, reservation_timestamp: None,
    )

    def fake_request_and_save_generated_image(**kwargs):
        recorded_request.update(kwargs)
        return {
            "status": "generated",
            "count": 1,
            "image": {},
            "message": "Generated image is ready and will be sent to Telegram.",
        }

    monkeypatch.setattr(
        image_tools,
        "_request_and_save_generated_image",
        fake_request_and_save_generated_image,
    )
