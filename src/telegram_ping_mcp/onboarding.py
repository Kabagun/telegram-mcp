"""Readable Telegram instructions and explicit formatting for connecting an AI chat."""

import unicodedata
from urllib.parse import urlsplit

from .config import ConfigError, normalize_public_mcp_url

PRIVACY_NOTICE = (
    "Плагин работает только с вашим чатом с ботом. Он не входит в Telegram-аккаунт "
    "и не видит остальные переписки, контакты и группы. Подключённое AI-приложение "
    "может читать сообщения и файлы, которые вы отправляете этому боту, "
    "и присылать вам ответы. Другие пользователи не видят ваши данные. "
    "Передача защищена HTTPS, доступ подтверждаете вы. "
    "Отключить — /unlink."
)

_NAME = "Telegram"
_DESCRIPTION = "Уведомления, ответы и файлы через Telegram-бота."
_CLIENT_HEADINGS = {
    "chatgpt": "Подключение к ChatGPT",
    "claude": "Подключение к Claude",
    "other": "Другое AI-приложение",
}
CALLBACK_ACTIONS = frozenset(
    {
        "ui:connect",
        "ui:chatgpt",
        "ui:claude",
        "ui:other",
        "ui:help_chatgpt",
        "ui:help_claude",
        "ui:help_other",
        "ui:privacy",
        "ui:unlink",
        "ui:unlink_confirm",
        "ui:cancel",
    }
)
BUTTON_ACTIONS = {
    "🔌 Подключить AI-приложение": "ui:connect",
    "🔗 Отключить всё": "ui:unlink",
}
_SECTION_HEADINGS = {
    *_CLIENT_HEADINGS.values(),
    "Настройки плагина",
    "🔑 Код подключения",
}


def _icon_download_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    hostname = f"[{hostname}]" if ":" in hostname else hostname.encode("idna").decode("ascii")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{hostname}{port}/assets/telegram-icon.png"


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def onboarding_presentation(text: str) -> tuple[list[dict], dict | None]:
    """Format a completed guide, including an already-issued personal login code.

    Telegram measures entity offsets in UTF-16 units. Only our known guide lines
    are formatted; user messages and short command statuses receive no markup.
    The caller must pass the final text so login codes retain exact offsets.
    """
    lines = text.split("\n")
    client = next((key for key, heading in _CLIENT_HEADINGS.items() if heading in lines), None)
    if client is None:
        return [], None

    entities = []
    values = {}
    offset = 0
    for content in lines:
        if content in _SECTION_HEADINGS:
            entities.append({"type": "bold", "offset": offset, "length": _utf16_length(content)})

        field = None
        value = ""
        prefix = ""
        for label, expected in (("Название: ", _NAME), ("Описание: ", _DESCRIPTION)):
            if content == label + expected:
                field, prefix, value = label, label, expected
                break
        if content.startswith("URL-адрес сервера: "):
            prefix = "URL-адрес сервера: "
            value = content[len(prefix) :]
            try:
                endpoint = normalize_public_mcp_url(value)
            except ConfigError:
                pass
            else:
                if endpoint:
                    field = prefix
        if content.startswith("Код входа: "):
            prefix = "Код входа: "
            value = content[len(prefix) :]
            if (
                value.strip()
                and len(value) <= 128
                and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
            ):
                field = prefix
        if field is not None:
            entities.append(
                {
                    "type": "code",
                    "offset": offset + _utf16_length(prefix),
                    "length": _utf16_length(value),
                }
            )
            values[field] = value
        offset += _utf16_length(content) + 1

    buttons = []
    for prefix, label in (
        ("Название: ", "📋 Название"),
        ("Описание: ", "📋 Описание"),
        ("URL-адрес сервера: ", "📋 Адрес"),
        ("Код входа: ", "📋 Код"),
    ):
        value = values.get(prefix)
        if value and _utf16_length(value) <= 256:
            buttons.append({"text": label, "copy_text": {"text": value}})
    keyboard = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    keyboard.append(
        [
            {"text": "Инструкция", "callback_data": f"ui:help_{client}"},
            {"text": "Приватность", "callback_data": "ui:privacy"},
        ]
    )
    return entities, {"inline_keyboard": keyboard} if keyboard else None


def command_menu(*, linked: bool = False) -> list[dict[str, str]]:
    """Build a fresh Telegram command list for the current binding state."""
    connect = {"command": "connect", "description": "Подключить AI-приложение"}
    if not linked:
        return [connect]
    return [
        connect,
        {"command": "unlink", "description": "Отключить все AI-приложения"},
    ]


def intro_text(*, linked: bool) -> str:
    """Introduce the product briefly, with actions appropriate to the binding."""
    actions = (
        "Добавьте ещё одно AI-приложение или отключите все подключения кнопками ниже."
        if linked
        else "Нажмите «🔌 Подключить AI-приложение» ниже."
    )
    return (
        "AI Connector соединяет Telegram с вашим AI-чатом. Чат может присылать "
        "уведомления и сообщения, читать ваши ответы и файлы.\n\n" + actions
    )


def onboarding_text(
    public_mcp_url: str = "",
    *,
    linked: bool = False,
    login_code: str | None = None,
    client: str = "chatgpt",
) -> str:
    """Return one plain-text Telegram message, also safe with a maximum-length URL."""
    endpoint = normalize_public_mcp_url(public_mcp_url)
    if client not in _CLIENT_HEADINGS:
        raise ValueError("Unknown AI application.")
    if login_code is not None and (
        not isinstance(login_code, str)
        or not login_code.strip()
        or len(login_code) > 128
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in login_code)
    ):
        raise ValueError("Login code is invalid.")
    address = endpoint or "адрес подключения запросите у владельца."
    code_section = (
        f"🔑 Код подключения\nКод входа: {login_code}\nБез срока действия · одноразовый.\n\n"
        if login_code is not None
        else ""
    )
    return (
        f"{_CLIENT_HEADINGS[client]}\n\n"
        "Настройки плагина\n"
        f"Название: {_NAME}\n"
        f"URL-адрес сервера: {address}\n"
        "Аутентификация: OAuth\n\n"
        f"{code_section}"
        "Вставьте код в окне подключения.\n"
        "📎 Иконка PNG — файлом ниже."
    )


def menu_keyboard(*, linked: bool = False) -> dict:
    rows = [[{"text": "🔌 Подключить AI-приложение"}]]
    if linked:
        rows.append([{"text": "🔗 Отключить всё"}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def client_picker() -> tuple[str, dict]:
    return "Куда подключаем Telegram?", {
        "inline_keyboard": [
            [
                {"text": "ChatGPT", "callback_data": "ui:chatgpt"},
                {"text": "Claude", "callback_data": "ui:claude"},
            ],
            [{"text": "Другое", "callback_data": "ui:other"}],
        ]
    }


def help_presentation(client: str, public_mcp_url: str = "") -> tuple[str, dict | None]:
    descriptions = {
        "chatgpt": (
            "Как подключить ChatGPT\n\n"
            "1. chatgpt.com → Плагины → + → Добавить пользовательский сервер MCP.\n"
            "2. Перенесите название, URL и OAuth из карточки настроек.\n"
            "3. Создайте плагин и вставьте код в окне подключения.\n\n"
            "Если нужен значок, загрузите PNG-файл из этого чата."
        ),
        "claude": (
            "Как подключить Claude\n\n"
            "claude.ai → Settings → Connectors → Add custom connector.\n\n"
            "Укажите название и URL из карточки настроек. "
            "В окне OAuth введите код подключения."
        ),
        "other": (
            "Как подключить другое AI-приложение\n\n"
            "Добавьте удалённый MCP-сервер по URL из карточки настроек. "
            "Выберите OAuth и введите код в открывшемся окне.\n\n"
            "Модель выбирается в вашем AI-приложении."
        ),
    }
    if client not in descriptions:
        raise ValueError("Unknown AI application.")
    row = []
    if client in {"chatgpt", "claude"}:
        row.append(
            {
                "text": "Открыть сайт",
                "url": "https://chatgpt.com/" if client == "chatgpt" else "https://claude.ai/",
            }
        )
    endpoint = normalize_public_mcp_url(public_mcp_url)
    if endpoint:
        row.append({"text": "Скачать PNG", "url": _icon_download_url(endpoint)})
    return descriptions[client], {"inline_keyboard": [row]} if row else None


def privacy_presentation() -> tuple[str, None]:
    text = PRIVACY_NOTICE.replace(
        "Отключить — /unlink.", "Отключить связь можно кнопкой «🔗 Отключить всё»."
    )
    return "Доступ и приватность\n\n" + text, None


def unlink_presentation() -> tuple[str, dict]:
    return (
        "Отключить все AI-приложения?\n\nВсе подключения и коды входа для этого чата "
        "будут удалены. Подключиться заново можно кнопкой «🔌 Подключить AI-приложение».",
        {
            "inline_keyboard": [
                [
                    {"text": "Отключить всё", "callback_data": "ui:unlink_confirm"},
                    {"text": "Отмена", "callback_data": "ui:cancel"},
                ]
            ]
        },
    )
