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

<!-- codex-config:bots:begin -->

<!-- codex-config:source bots/AGENTS.md -->

## Navigation
- Use buttons for all user navigation and actions. Keep slash commands out of menus, help and notifications; retain `/start` only for Telegram's Start button.
- Reply keyboard: stable, collapsible main navigation; change it only with interaction mode and restore it on exit. No `is_persistent=True` in ordinary menus.
- Inline buttons: current-message actions and contextual Back within the section. Keep main navigation in the reply keyboard; edit the current message when possible.
## Forms and editors
- Keep cancellation only in the reply keyboard. Use one bound form message: edit it when possible; otherwise remove it before replacement. If removal fails, do not create a duplicate.
## Releases
- Successful production releases with user-visible changes: one polished Russian notification to all eligible users through the existing broadcaster; skip rollbacks. Honor suppression, postponement and customization.
- Deploy requests authorize drafting and sending after approval. After implementation and validation, get approval for the exact final message before deployment unless waived; renew approval for substantial edits. Then deploy → verify the new version is running and reachable → broadcast.
- Explain user benefits with readable spacing, grouped bullets and restrained emoji; technical details only when useful. Send silently when supported or required, idempotently once per version; retry only failed recipients. Report only sent/failed counts to the owner.

<!-- codex-config:bots:end -->
