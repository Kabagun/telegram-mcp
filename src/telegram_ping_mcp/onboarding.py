"""Short Telegram instructions for connecting an AI chat."""

from .config import normalize_public_mcp_url


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
        "Если приглашения нет, запросите его у владельца.\n\n"
    )
    address = f"Адрес: {endpoint}" if endpoint else "Адрес подключения запросите у владельца."
    return (
        invitation
        + "1. В ChatGPT (https://chatgpt.com/) откройте плагины и добавьте AI Connector. "
        "В Claude (https://claude.ai/) — настройки → Connectors → Add custom connector.\n"
        f"2. Укажите адрес подключения. {address}\n"
        "3. В открывшемся окне введите код из бота и разрешите доступ."
    )
