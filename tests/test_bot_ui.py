import pytest

from telegram_ping_mcp.onboarding import (
    BUTTON_ACTIONS,
    CALLBACK_ACTIONS,
    UNLINK_BUTTON_ACTIONS,
    client_picker,
    help_presentation,
    intro_text,
    menu_keyboard,
    onboarding_presentation,
    onboarding_text,
    privacy_presentation,
    settings_details_presentation,
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
    for linked in (False, True):
        keyboard = menu_keyboard(linked=linked)
        _validate_reply_markup(keyboard)
        assert len(keyboard["keyboard"]) == (2 if linked else 1)
        assert "is_persistent" not in keyboard
        for row in keyboard["keyboard"]:
            assert len(row) == 1
            assert all(item["text"] in BUTTON_ACTIONS for item in row)
    for text, markup in (
        client_picker(),
        help_presentation("chatgpt", "https://connector.example.test/mcp"),
        help_presentation("claude", "https://connector.example.test/mcp"),
        help_presentation("other", "https://connector.example.test/mcp"),
        privacy_presentation(),
        unlink_presentation(),
    ):
        assert len(text) < 1000
        if markup is not None:
            _validate_reply_markup(markup)
        for row in (markup or {}).get("inline_keyboard", []):
            for item in row:
                if "callback_data" in item:
                    assert item["callback_data"] in CALLBACK_ACTIONS
    assert "/start" not in intro_text(linked=True)
    assert "/connect" not in intro_text(linked=False)


def test_navigation_only_offers_actions_that_add_information_or_change_a_connection():
    assert set(BUTTON_ACTIONS) == {"🔌 Подключить AI-приложение", "🔗 Отключить всё"}
    assert not CALLBACK_ACTIONS.intersection({"ui:menu", "ui:help", "ui:start", "ui:stop"})
    assert {
        button["callback_data"] for row in client_picker()[1]["inline_keyboard"] for button in row
    } == {"ui:chatgpt", "ui:claude", "ui:other"}
    _, markup = onboarding_presentation(
        onboarding_text("https://connector.example.test/mcp", login_code="SYNTHETIC-CODE")
    )
    actions = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]
    assert actions == ["ui:help_chatgpt", "ui:privacy"]
    assert help_presentation("other")[1] is None
    assert privacy_presentation()[1] is None
    assert "все AI-приложения" in unlink_presentation()[0]
    _, confirmation = unlink_presentation()
    assert "inline_keyboard" not in confirmation
    assert {button["text"] for row in confirmation["keyboard"] for button in row} == set(
        UNLINK_BUTTON_ACTIONS
    )


@pytest.mark.parametrize("client", ["chatgpt", "claude", "other"])
@pytest.mark.parametrize("section", ["help", "privacy"])
def test_contextual_details_keep_exact_copy_values_and_utf16_entities_when_collapsed(
    client, section
):
    endpoint = "https://пример.рф/🙂/mcp"
    code = "SYNTHETIC-🙂-<&_`>"
    base = onboarding_text(endpoint, login_code=code, client=client)
    base_entities, base_markup = onboarding_presentation(base)
    action = f"ui:help_{client}" if section == "help" else "ui:privacy"

    text, entities, markup = settings_details_presentation(base, action, endpoint)

    assert text.startswith(base + "\n\n")
    _validate_reply_markup(markup)
    assert len(markup["inline_keyboard"]) <= 4
    assert all(len(row) <= 3 for row in markup["inline_keyboard"])
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert {button["copy_text"]["text"] for button in buttons if "copy_text" in button} == {
        "Telegram",
        endpoint,
        code,
    }
    assert [button["callback_data"] for button in buttons if "callback_data" in button] == [
        "ui:details_back"
    ]
    encoded = text.encode("utf-16-le")
    formatted = {
        encoded[item["offset"] * 2 : (item["offset"] + item["length"]) * 2].decode(
            "utf-16-le"
        ): item["type"]
        for item in entities
    }
    assert formatted[code] == "code"
    assert formatted[endpoint] == "code"
    heading = (
        "Доступ и приватность"
        if section == "privacy"
        else help_presentation(client)[0].split("\n", 1)[0]
    )
    assert formatted[heading] == "bold"
    assert settings_details_presentation(text, "ui:details_back", endpoint) == (
        base,
        base_entities,
        base_markup,
    )
