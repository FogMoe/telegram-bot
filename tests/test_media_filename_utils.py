from features.ai.tools.filename_utils import (
    MAX_MEDIA_FILENAME_CHARS,
    prompt_to_filename,
)


def test_prompt_to_filename_sanitizes_invalid_characters():
    filename = prompt_to_filename(
        ' hello:/world\n* ',
        ".mp3",
        fallback_base="generated_audio",
    )

    assert filename == "hello world.mp3"


def test_prompt_to_filename_truncates_long_names_with_suffix():
    filename = prompt_to_filename(
        "a" * 300,
        ".mp3",
        fallback_base="generated_audio",
    )

    assert len(filename) <= MAX_MEDIA_FILENAME_CHARS
    assert filename.endswith("....mp3")
