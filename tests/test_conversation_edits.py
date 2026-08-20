from types import SimpleNamespace

import pytest

from features.conversation import messages as conversation


@pytest.fixture(autouse=True)
def _clear_message_fingerprints():
    conversation._MESSAGE_CONTENT_FINGERPRINTS.clear()
    yield
    conversation._MESSAGE_CONTENT_FINGERPRINTS.clear()


def _message(
    *,
    message_id=10,
    text=None,
    caption=None,
    photo=(),
    sticker=None,
):
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=caption,
        photo=photo,
        sticker=sticker,
    )


def _update(message, *, edited=False, chat_id=20):
    return SimpleNamespace(
        message=None if edited else message,
        edited_message=message if edited else None,
        effective_chat=SimpleNamespace(id=chat_id),
    )


def test_unchanged_edited_text_is_ignored():
    original = _message(text="hello")
    unchanged_edit = _message(text="hello")

    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(original)
    )
    assert conversation._record_message_content_and_check_unchanged_edit(
        _update(unchanged_edit, edited=True)
    )


def test_changed_edited_text_is_processed_and_becomes_new_baseline():
    original = _message(text="hello")
    changed_edit = _message(text="hello again")
    repeated_edit = _message(text="hello again")

    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(original)
    )
    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(changed_edit, edited=True)
    )
    assert conversation._record_message_content_and_check_unchanged_edit(
        _update(repeated_edit, edited=True)
    )


def test_changed_caption_or_photo_is_processed():
    original = _message(
        caption="before",
        photo=(SimpleNamespace(file_unique_id="photo-a"),),
    )
    changed_caption = _message(
        caption="after",
        photo=(SimpleNamespace(file_unique_id="photo-a"),),
    )
    changed_photo = _message(
        caption="after",
        photo=(SimpleNamespace(file_unique_id="photo-b"),),
    )

    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(original)
    )
    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(changed_caption, edited=True)
    )
    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(changed_photo, edited=True)
    )


def test_same_message_id_in_another_chat_is_not_ignored():
    original = _message(text="hello")
    other_chat_edit = _message(text="hello")

    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(original, chat_id=20)
    )
    assert not conversation._record_message_content_and_check_unchanged_edit(
        _update(other_chat_edit, edited=True, chat_id=21)
    )
