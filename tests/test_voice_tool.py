import pytest
from pydantic import ValidationError

from features.ai import tool_runner
from features.ai.tools import voice_tools
from features.ai.tools.models import GenerateVoiceArgs, parameters_schema


def test_generate_voice_schema_requires_bounded_text():
    schema = parameters_schema(GenerateVoiceArgs)

    text_schema = schema["properties"]["text"]

    assert "text" in schema.get("required", [])
    assert text_schema["minLength"] == 1
    assert text_schema["maxLength"] == voice_tools.MAX_VOICE_TEXT_CHARS


def test_generate_voice_validation_rejects_long_text():
    with pytest.raises(ValidationError):
        GenerateVoiceArgs.model_validate(
            {"text": "a" * (voice_tools.MAX_VOICE_TEXT_CHARS + 1)}
        )


def test_generate_voice_tool_uses_fish_audio_defaults(monkeypatch):
    recorded_request = {}
    _prepare_successful_voice_tool(monkeypatch, recorded_request)

    result = voice_tools.generate_voice_tool(text="hello")

    assert result["status"] == "generated"
    assert recorded_request["api_key"] == "token"
    assert recorded_request["model"] == voice_tools.DEFAULT_FISH_AUDIO_MODEL
    assert recorded_request["reference_id"] == voice_tools.DEFAULT_FISH_AUDIO_REFERENCE_ID
    assert recorded_request["timeout"] == voice_tools.DEFAULT_VOICE_TIMEOUT_SECONDS


def test_generate_voice_public_result_hides_audio_id():
    result = tool_runner._public_tool_result(
        "generate_voice",
        {
            "status": "generated",
            "audios": [{"audio_id": "secret-audio-id"}],
        },
    )

    assert result["status"] == "generated"
    assert "audio" in result["message"].lower()
    assert "forward" not in result["message"].lower()
    assert "secret-audio-id" not in str(result)


def test_save_audio_uses_input_text_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_tools, "GENERATED_AUDIO_DIR", tmp_path)

    result = voice_tools._save_audio(
        audio_bytes=b"audio",
        text="hello:/world",
        content_type="audio/opus",
    )

    assert result["filename"] == "hello world.ogg"
    assert result["format"] == "opus"
    assert result["mime_type"] == "audio/ogg"
    voice_tools._GENERATED_AUDIO_FILES.pop(result["audio_id"], None)


def _prepare_successful_voice_tool(monkeypatch, recorded_request):
    monkeypatch.setattr(voice_tools, "_cleanup_expired_generated_audio", lambda: None)
    monkeypatch.setattr(voice_tools.config, "FISH_AUDIO_API_KEY", "token")
    monkeypatch.setattr(
        voice_tools.config,
        "FISH_AUDIO_MODEL",
        voice_tools.DEFAULT_FISH_AUDIO_MODEL,
    )
    monkeypatch.setattr(
        voice_tools.config,
        "FISH_AUDIO_REFERENCE_ID",
        voice_tools.DEFAULT_FISH_AUDIO_REFERENCE_ID,
    )
    monkeypatch.setattr(voice_tools, "_get_request_user_id", lambda: 123)
    monkeypatch.setattr(
        voice_tools,
        "_reserve_voice_generation",
        lambda user_id: (True, 1.0, None),
    )
    monkeypatch.setattr(
        voice_tools,
        "_release_voice_generation",
        lambda user_id, reservation_timestamp: None,
    )

    def fake_request_and_save_generated_voice(**kwargs):
        recorded_request.update(kwargs)
        return {
            "status": "generated",
            "count": 1,
            "audios": [],
            "message": "Generated audio is ready and will be sent to Telegram.",
        }

    monkeypatch.setattr(
        voice_tools,
        "_request_and_save_generated_voice",
        fake_request_and_save_generated_voice,
    )
