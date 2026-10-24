# systemd user deployment

The service runs as `apps` from `/srv/apps/telegram-ping-mcp/app` and listens on
`127.0.0.1:18007`. Confirm that the port is available before installation. Keep
the listener on loopback and configure HTTPS ingress before connecting a remote
MCP client. The server does not implement OAuth; clients need the static Bearer
token.

Copy the application, including `pyproject.toml`, `uv.lock`, `README.md`, `src/`
and `deploy/`, into that directory. Do not upload `.env`, local runtime data,
`temp/` or a Windows `.venv`. Run:

```sh
sh deploy/install-user-service.sh
```

The installer uses the existing `uv`, installs locked production dependencies in
the project environment and installs the user unit. It preserves an existing
configuration and leaves the service stopped. No root commands are needed. The
database lives in `/srv/apps/telegram-ping-mcp/data`, outside the application
checkout; the installer does not require write access to `~/.local/share`.

Populate `/home/apps/.config/telegram-ping-mcp/server.env` through a protected
channel. Give it mode `0600` and keep its parent and the data directory at `0700`.
Use a separate randomly generated MCP token of at least 32 characters. Add the
chosen public hostname to `MCP_ALLOWED_HOSTS` when the HTTPS proxy is configured.
Retain the loopback entry for health probes. Do not expose the service directly
or include credentials in command arguments, shell history or logs.

Validate the unit with `systemd-analyze --user verify` and run the application's
offline `doctor` with the protected environment. Then activate:

```sh
systemctl --user enable --now telegram-ping-mcp.service
systemctl --user is-active telegram-ping-mcp.service
curl --fail --silent http://127.0.0.1:18007/health
```

The first start calls Telegram `getMe` and starts long polling. Use one service
per bot; an existing webhook or another poller must be resolved before starting.
Activation does not send a Telegram message. Delivery tests require a named
destination and explicit authorization.

The unauthenticated `/health` probe should return 200 after startup. A request to
`/mcp` without credentials must remain unauthorized. Check authenticated MCP
initialization and `tools/list` through the protected client configuration; these
checks do not require sending a message. Never print the Bearer header.

User services survive logout only when the host already enables lingering for
`apps`. Inspect `loginctl show-user apps -p Linger`; if it is disabled, an
administrator must arrange the intended persistent user service policy.

For an update, stop the service before replacing the application, synchronize
the locked production dependencies, then restart and repeat the readiness and
authentication checks. Preserve the external configuration and SQLite data. For
rollback, restore the previous application package and restart; preserve the
database and keep a consistent backup before any future schema migration.

## Файлы до 100 МиБ

В облачном режиме Telegram ограничивает скачивание 20 МБ. Предел MCP уже равен
100 МиБ, но фактически использовать его можно с собственным
[Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api) в режиме
`--local`. Этому серверу требуются отдельные `TELEGRAM_API_ID` и
`TELEGRAM_API_HASH` с `my.telegram.org`, дополнительно к токену бота. Личные
сессии пользователя и MTProto-клиент в MCP не создаются.

Подготовлен шаблон `telegram-bot-api.service`: бинарник устанавливается в
`/home/apps/Programs/telegram-bot-api/bin/telegram-bot-api`, слушает только
`127.0.0.1:18008` и работает под `apps`. До установки проверьте свободный порт,
соберите официальный сервер по его инструкции и создайте закрытые каталоги
`/srv/apps/telegram-ping-mcp/bot-api` и `bot-api-temp` с правами `0700`.
Секреты хранятся в `~/.config/telegram-ping-mcp/bot-api.env` с правами `0600`;
образец без значений находится в `bot-api.env.example`. Шаблон автоматически
не устанавливается и не запускается обычным установщиком MCP.

Для переключения остановите MCP poller. Следуя инструкции Telegram о миграции,
выполните `logOut` бота на облачном API через защищённый административный клиент,
запустите локальный Bot API и измените защищённый `server.env`:

```dotenv
TELEGRAM_API_BASE_URL=http://127.0.0.1:18008
TELEGRAM_LOCAL_FILES_ROOT=/srv/apps/telegram-ping-mcp/bot-api
MAX_ATTACHMENT_BYTES=104857600
```

Сохраните существующие bot/MCP-токены и путь к SQLite. Затем запустите MCP,
проверьте readiness и только после этого пробуйте файл больше 20 МБ.
Для корректного порядка запуска после перезагрузки установите
`telegram-bot-api.service` в `~/.config/systemd/user/`, а `local-api.conf` — в
`~/.config/systemd/user/telegram-ping-mcp.service.d/local-api.conf`; выполните
`systemctl --user daemon-reload` и включите оба сервиса. Drop-in устанавливается
только при использовании локального API, облачный режим от него не зависит.
Локальный Bot API сохраняет скачанные файлы в собственном закрытом кэше;
учитывайте место на диске и персональные данные в резервных копиях.

Крупный файл выдаётся через защищённый HTTP-маршрут `/attachments/{id}`.
Он требует тот же Bearer-заголовок, что `/mcp`, и не содержит секрет в URL.
В MCP-контекст возвращаются только метаданные, чтобы файл на 100 МиБ не
превратился в огромную строку Base64 в контексте модели.
