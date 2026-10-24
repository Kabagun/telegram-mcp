"""Public, credential-free Telegram instructions for connecting an AI client."""

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
        "уведомления и сообщения, читать ваши ответы и файлы. "
        "Модель выбирается в AI-приложении.\n\n" + actions
    )


def onboarding_text(public_mcp_url: str = "") -> str:
    """Return one plain-text Telegram message, also safe with a maximum-length URL."""
    endpoint = normalize_public_mcp_url(public_mcp_url)
    address = (
        f"Адрес MCP: {endpoint}" if endpoint else "Адрес MCP и доступ получите у администратора."
    )
    return (
        "AI Connector — мост между Telegram и вашим AI-чатом.\n"
        "Модель выбирается в AI-приложении; ключ API модели боту не нужен.\n\n"
        f"{address}\n\n"
        "1. Подключите HTTP MCP в AI-приложении. Отдельный токен доступа MCP получите "
        "у администратора и вводите только в настройках клиента.\n"
        "2. Попросите подключённый AI-чат создать персональную ссылку, откройте её "
        "в Telegram и нажмите Start.\n"
        "3. Отправьте сюда сообщение или файл, затем попросите AI-чат: "
        "«Проверь входящие Telegram и ответь». Бот сам не пробуждает AI-чат.\n\n"
        "Claude: https://claude.ai/ → Customize → Connectors → Add custom connector. "
        "Укажите Server URL; Authentication: No Sign In; в Request headers добавьте "
        "Authorization: Bearer <отдельный токен MCP> (это шаблон).\n"
        "https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp\n\n"
        "ChatGPT: https://chatgpt.com/ → Plugins → Add custom MCP server. "
        "Веб-версия требует OAuth; этот сервер его пока не поддерживает, "
        "поэтому прямое подключение сейчас недоступно.\n"
        "https://developers.openai.com/api/docs/guides/custom-mcp-server\n\n"
        "Codex поддерживает Streamable HTTP с Bearer-заголовком:\n"
        "https://learn.chatgpt.com/docs/extend/mcp\n"
        "Другой MCP-клиент: Streamable HTTP и заголовок Authorization: Bearer "
        "<отдельный токен MCP>. Выбор модели — в клиенте.\n\n"
        "/connect — инструкция подключения. Команды управления появятся после привязки."
    )
