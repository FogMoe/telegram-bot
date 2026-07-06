from features.ai.message_content import (
    content_to_text,
    messages_have_images,
    strip_image_content,
)


def test_messages_have_images_detects_multimodal_image_items():
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        },
    ]

    assert messages_have_images(messages) is True


def test_content_to_text_keeps_only_text_parts_in_order():
    content = [
        {"type": "text", "text": "first"},
        {
            "type": "image_url",
            "image_url": {"url": "https://example.test/a.png"},
        },
        {"type": "text", "text": "second"},
        "ignored raw item",
    ]

    assert content_to_text(content) == "first\nsecond"


def test_strip_image_content_replaces_images_without_mutating_original_message():
    original_content = [
        {"type": "text", "text": "caption"},
        {
            "type": "image_url",
            "image_url": {"url": "https://example.test/a.png"},
        },
    ]
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": original_content},
    ]

    stripped = strip_image_content(messages)

    assert stripped[0] == messages[0]
    assert stripped[1] == {"role": "user", "content": "caption"}
    assert messages[1]["content"] is original_content
