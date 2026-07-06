from core import bot_conversation


def test_format_xml_message_includes_current_message_id():
    result = bot_conversation._format_xml_message(
        chat_type="private",
        chat_title=None,
        timestamp="2026-07-06 20:10:00",
        user_name="kc",
        message_text="hello",
        message_id=1201,
    )

    first_line = result.splitlines()[0]
    assert 'message_id="1201"' in first_line
    assert 'edited="' not in first_line
    assert "<message>hello</message>" in result


def test_format_xml_message_marks_edited_messages():
    result = bot_conversation._format_xml_message(
        chat_type="private",
        chat_title=None,
        timestamp="2026-07-06 20:10:00",
        user_name="kc",
        message_text="updated",
        message_id=1201,
        edited=True,
        edited_at="2026-07-06 20:10:18",
    )

    first_line = result.splitlines()[0]
    assert 'message_id="1201"' in first_line
    assert 'edited="true"' in first_line
    assert 'edited_at="2026-07-06 20:10:18"' in first_line


def test_forward_message_id_stays_in_forward_metadata():
    result = bot_conversation._format_xml_message(
        chat_type="private",
        chat_title=None,
        timestamp="2026-07-06 20:10:00",
        user_name="kc",
        message_text="forwarded",
        message_id=1201,
        forward_type="channel",
        forward_chat="@some_channel",
        forward_message_id="456",
    )

    lines = result.splitlines()
    assert 'message_id="1201"' in lines[0]
    assert 'message_id="456"' not in lines[0]
    assert '<forward type="channel" chat="@some_channel" message_id="456" />' in lines[1]
