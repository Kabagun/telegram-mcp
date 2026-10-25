"""Short Telegram instructions for connecting an AI chat."""

from urllib.parse import urlsplit

from .config import normalize_public_mcp_url

PRIVACY_NOTICE = (
    "Плагин работает только с вашим чатом с ботом. Он не входит в Telegram-аккаунт "
    "и не видит остальные переписки, контакты и группы. Подключённое AI-приложение "
    "может читать сообщения и файлы, которые вы отправляете этому боту, "
    "и присылать вам ответы. Другие пользователи не видят ваши данные. "
    "Передача защищена HTTPS, доступ подтверждаете вы. "
    "Отключить — /unlink."
)


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
        "AI Connector соединяет Telegram с вашим AI-чатом. Чат может присылать "
        "уведомления и сообщения, читать ваши ответы и файлы.\n\n" + actions
    )


def onboarding_text(public_mcp_url: str = "", *, linked: bool = False) -> str:
    """Return one plain-text Telegram message, also safe with a maximum-length URL."""
    endpoint = normalize_public_mcp_url(public_mcp_url)
    invitation = (
        ""
        if linked
        else "Сначала откройте личное приглашение в Telegram и нажмите Start. "
        "Если приглашения нет, запросите его у владельца. "
        "Затем отправьте /connect, чтобы получить код входа.\n\n"
    )
    if endpoint:
        parsed = urlsplit(endpoint)
        # The endpoint stays exactly as configured. Canonicalize the icon's host
        # so a long Unicode hostname cannot duplicate most of the message limit.
        hostname = parsed.hostname or ""
        hostname = f"[{hostname}]" if ":" in hostname else hostname.encode("idna").decode("ascii")
        port = f":{parsed.port}" if parsed.port is not None else ""
        icon = (
            f"скачайте PNG https://{hostname}{port}/assets/telegram-icon.png "
            "и загрузите файл (256×256, до 10 КБ)."
        )
        address = endpoint
    else:
        icon = "PNG-иконку запросите у владельца."
        address = "адрес подключения запросите у владельца."
    return (
        invitation + "ChatGPT: https://chatgpt.com/ → Плагины → + → "
        "Добавить пользовательский сервер MCP.\n"
        "Название: AI Connector\n"
        "Описание: Уведомления, ответы и файлы через Telegram-бота.\n"
        f"Значок: {icon}\n"
        "Подключение: выберите «URL-адрес сервера».\n"
        f"URL-адрес сервера: {address}\n"
        "Аутентификация: OAuth\n"
        "В расширенных настройках оставьте DCR и telegram. "
        "Остальные поля заполнятся автоматически; Client ID и Client Secret не нужны. "
        "OIDC не включайте.\n"
        "Отметьте «Я понимаю и хочу продолжить» → «Создать в виде плагина». Установите плагин, "
        "в AI-чате выберите @AI Connector. В окне подключения введите код из бота "
        "и разрешите доступ.\n\n"
        "Claude: https://claude.ai/ → Settings → Connectors → Add custom connector; "
        "Name: AI Connector; Server URL: тот же адрес; Authentication: OAuth.\n\n" + PRIVACY_NOTICE
    )
