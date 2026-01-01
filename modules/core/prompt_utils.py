from typing import Optional


def xml_escape(value: str) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_user_state_prompt(
    *,
    user_coins: int,
    user_permission: int,
    user_affection: int,
    impression: str,
    personal_info: str = "",
    diary_exists: bool = False,
) -> str:
    permission_labels = {
        0: "Normal",
        1: "Advanced",
        2: "Maximum",
    }
    permission_label = permission_labels.get(user_permission, "Unknown")
    attrs = [
        ("coins", str(user_coins)),
        ("permission", str(user_permission)),
        ("permission_label", permission_label),
        ("affection", str(user_affection)),
        ("diary_exists", "true" if diary_exists else "false"),
    ]
    attr_text = " ".join(
        f'{key}="{xml_escape(value)}"' for key, value in attrs if value
    )
    lines = [f"<user_state {attr_text} />"]
    if impression or personal_info:
        lines.append("<user_profile>")
        if impression:
            lines.append(f"  <impression>{xml_escape(impression)}</impression>")
        if personal_info:
            lines.append(f"  <personal_info>{xml_escape(personal_info)}</personal_info>")
        lines.append("</user_profile>")
    return "\n".join(lines)
