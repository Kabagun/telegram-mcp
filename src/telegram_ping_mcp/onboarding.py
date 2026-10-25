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
_INVITATION_HEADING = "🔑 До подключения"
_CHATGPT_HEADING = "1️⃣ Откройте ChatGPT"
_SECTION_HEADINGS = {
    _INVITATION_HEADING,
    _CHATGPT_HEADING,
    "2️⃣ Заполните поля",
    "3️⃣ Проверьте настройки",
    "4️⃣ Завершите подключение",
    "Claude",
    "🔒 Доступ и приватность",
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
    The caller must pass the final text so appended codes retain exact offsets.
    """
    lines = text.split("\n")
    if _CHATGPT_HEADING not in lines:
        return [], None

    entities = []
    values = {}
    endpoint = ""
    linked = _INVITATION_HEADING not in lines
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
        if linked and content.startswith("Код входа: "):
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
    if endpoint:
        keyboard.append([{"text": "Скачать PNG", "url": _icon_download_url(endpoint)}])
    return entities, {"inline_keyboard": keyboard} if keyboard else None


def command_menu(*, linked: bool = False) -> list[dict[str, str]]:
    """Build a fresh Telegram command list for the current binding state."""
    connect = {"command": "connect", "description": "Как подключить AI-чат"}
    if not linked:
        return [connect]
    return [
        {"command": "start", "description": "Включить уведомления"},
        connect,
        {"command": "stop", "description": "Отключить уведомления"},
        {"command": "unlink", "description": "Удалить привязку"},
    ]


def intro_text(*, linked: bool) -> str:
    """Introduce the product briefly, with actions appropriate to the binding."""
    actions = (
        "/start — включить уведомления; /stop — отключить; /unlink — удалить привязку. "
        "Инструкция подключения — /connect."
        if linked
        else "Чтобы подключить AI-чат, используйте /connect."
    )
    return (
        "iConnector соединяет Telegram с вашим AI-чатом. Чат может присылать "
        "уведомления и сообщения, читать ваши ответы и файлы.\n\n" + actions
    )


def onboarding_text(public_mcp_url: str = "", *, linked: bool = False) -> str:
    """Return one plain-text Telegram message, also safe with a maximum-length URL."""
    endpoint = normalize_public_mcp_url(public_mcp_url)
    invitation = (
        ""
        if linked
        else _INVITATION_HEADING + "\n"
        "Откройте личное приглашение в Telegram и нажмите Start.\n"
        "Если приглашения нет, запросите его у владельца.\n"
        "Затем отправьте /connect, чтобы получить код входа.\n\n"
    )
    address = endpoint or "адрес подключения запросите у владельца."
    privacy = (
        "• Плагин работает только с вашим чатом с ботом. Он не входит в Telegram-аккаунт "
        "и не видит остальные переписки, контакты и группы.\n"
        "• Подключённое AI-приложение может читать сообщения и файлы, которые вы отправляете "
        "этому боту, и присылать вам ответы.\n"
        "• Другие пользователи не видят ваши данные. Передача защищена HTTPS, "
        "доступ подтверждаете вы.\n"
        "• Отключить — /unlink."
    )
    return (
        invitation + _CHATGPT_HEADING + "\n"
        "https://chatgpt.com/ → Плагины → +\n"
        "Добавить пользовательский сервер MCP\n\n"
        "2️⃣ Заполните поля\n"
        f"Название: {_NAME}\n"
        f"Описание: {_DESCRIPTION}\n"
        "Подключение: URL-адрес сервера\n"
        f"URL-адрес сервера: {address}\n"
        "Аутентификация: OAuth\n\n"
        "3️⃣ Проверьте настройки\n"
        "DCR: автоматически\n"
        "Scope: telegram\n"
        "Client ID и Client Secret: не нужны\n"
        "OIDC: выключен\n\n"
        "4️⃣ Завершите подключение\n"
        "«Я понимаю и хочу продолжить» → «Создать в виде плагина».\n"
        "Установите плагин. В AI-чате выберите @Telegram.\n"
        "В окне подключения введите код из бота и разрешите доступ.\n\n"
        "📎 Иконка PNG — файлом ниже.\n"
        "Загрузите её в «Значок» (256×256, до 10 КБ).\n\n"
        "Claude\n"
        "https://claude.ai/ → Settings → Connectors → Add custom connector.\n"
        "Name: Telegram; Server URL: адрес выше; Authentication: OAuth.\n\n"
        "🔒 Доступ и приватность\n" + privacy
    )
