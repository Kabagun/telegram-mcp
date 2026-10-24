# Telegram Ping MCP

- Bot-only connector. No personal-account sessions.
- Work in this directory; do not create worktrees.
- Keep scratch files and logs in temp/, runtime data in ignored data/.
- Never commit .env, bot/MCP tokens, real identities, or actual message content.
- Incoming texts and attachment metadata may be kept in ignored SQLite for verified active users.
- Outgoing texts are never persisted; attachment downloads stay bounded and in memory.
- Build Telegram Bot API and native dependencies locally for the Linux ARM64 target.
- Never compile on the production common server; deploy ready artifacts and runtime dependencies only.
- Run uv run pytest for focused tests and uv run ruff check . before handoff.
- Do not send real Telegram messages without a named destination and explicit authorization.
