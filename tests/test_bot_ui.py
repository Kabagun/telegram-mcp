import pytest

from telegram_ping_mcp.onboarding import (
    BUTTON_ACTIONS,
    CALLBACK_ACTIONS,
    client_picker,
    help_presentation,
    intro_text,
    menu_keyboard,
    onboarding_presentation,
    onboarding_text,
    privacy_presentation,
    unlink_presentation,
)
from telegram_ping_mcp.telegram import _validate_reply_markup


@pytest.mark.parametrize("client", ["chatgpt", "claude", "other"])
def test_selected_client_card_is_short_copyable_and_keeps_details_separate(client):
    code = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567"
    text = onboarding_text("https://connector.example.test/mcp", login_code=code, client=client)
    assert len(text) < 500
    assert text.index("Аутентификация: OAuth") < text.index("Код входа: ")
    assert text.index("Код входа: ") < text.index("Иконка PNG")
    assert "Без срока действия" in text
    for removed in ("DCR", "Client Secret", "/connect", "/unlink", "Контакты", "Плагины →"):
        assert removed not in text.replace("https://connector.example.test/mcp", "")
    entities, markup = onboarding_presentation(text)
    _validate_reply_markup(markup)
    assert any(item["type"] == "bold" for item in entities)
    buttons = [item for row in markup["inline_keyboard"] for item in row]
    assert any(item.get("copy_text", {}).get("text") == code for item in buttons)
    assert any(item.get("callback_data") == f"ui:help_{client}" for item in buttons)
    assert any(item.get("callback_data") == "ui:privacy" for item in buttons)


def test_all_menu_and_details_buttons_match_the_transport_and_static_actions():
    for linked, active in ((False, True), (True, True), (True, False)):
        keyboard = menu_keyboard(linked=linked, active=active)
        _validate_reply_markup(keyboard)
        for row in keyboard["keyboard"]:
            assert all(item["text"] in BUTTON_ACTIONS for item in row)
    for text, markup in (
        client_picker(),
        help_presentation(),
        help_presentation("chatgpt"),
        help_presentation("claude"),
        help_presentation("other"),
        privacy_presentation(),
        unlink_presentation(),
    ):
        assert len(text) < 1000
        _validate_reply_markup(markup)
        for row in markup["inline_keyboard"]:
            for item in row:
                if "callback_data" in item:
                    assert item["callback_data"] in CALLBACK_ACTIONS
    assert "/start" not in intro_text(linked=True)
    assert "/connect" not in intro_text(linked=False)
