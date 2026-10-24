# Deployment

## Current common server

Use the `appsctl` 2026.10.25.5 workflow as the authenticated `apps` deployment
account. Telegram runs under its separate runtime account. Perform all deployment
actions as `apps` using the approved workspace and `appsctl`, without sudo or
shared user services. The standalone installer and systemd/Bot API service
instructions below are legacy and must not be used on the current common server.

Inspect the existing registration and its assigned workspace before updating:

```sh
appsctl workspace telegram --json
appsctl status telegram --json
```

The workspace response identifies the source directory and separate dependency
directories. Use those returned paths; do not invent a checkout or dependency
location. The platform does not fetch Git
repositories or run package managers as root. Prepare code and dependencies with
the deployment owner's authorized permissions in the assigned workspace.
When copying into existing runtime-owned directories, preserve their ownership,
permissions and ACLs; write access does not grant permission to reset directory
metadata.

Build the application wheel locally and prepare the locked production dependency
set for the runtime's Python version and Linux ARM64 target. Transfer ready wheels
and any required native runtime libraries, then install from those artifacts into
the assigned dependency environment. Require binary dependencies and fail if an
artifact is missing; do not fall back to a source build. Build Telegram Bot API
and native dependencies locally as well. Never compile on the production server
or install build tools there.

Before replacing anything, retain a restorable previous release containing both
the source/code and its exact dependency set, including native runtime libraries.
Record how to restore both into their assigned paths. Keeping only an old wheel
is insufficient for rollback after dependency changes. Keep protected
configuration, SQLite data and persistent Bot API state outside source replacement;
preserve their existing paths, permissions and the native Bot API installation.
Take a consistent database backup before any schema migration.

After preparing the source and dependency directories, restart the application
with its saved declaration:

```sh
appsctl restart telegram --json
appsctl status telegram --json
```

To reapply the saved capability declaration and force a restart, use:

```sh
appsctl deploy ~/.config/app-platform/manifests/telegram.yaml --json
```

`restart` and `deploy` restart the bound application group and check readiness.
`apply` reconciles capabilities and can reuse a healthy unchanged process, so it
does not force an updated process to start. Confirm readiness and authenticated
MCP initialization through the protected client configuration after the update.
These checks do not require sending a Telegram message.

If an update fails, inspect only the application's bounded log tail:

```sh
appsctl logs telegram --lines 100
```

Request fewer lines if the response is truncated. Restore the previous code and
dependencies together, then run `restart` or deploy the saved declaration again
and repeat the readiness checks. The controller compensates publication and
registration changes, but cannot roll back arbitrary writes to application code,
dependencies or persistent state. Preserve configuration, SQLite and Bot API
state during recovery; use the migration's backup/restore procedure when needed.

Set the non-secret `PUBLIC_MCP_URL` to the public HTTPS MCP endpoint to include
it in `/connect`. Leaving it empty asks users to obtain the address
from the administrator. Never include an authentication token in this URL.

## Standalone systemd user installation (legacy)

Build Telegram Bot API and native dependencies on the local workstation for
Linux ARM64. The production server receives ready artifacts; do not compile
sources or install build tools there. Local cross-build scratch stays in the
project's ignored `temp/local-build/` environment.

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

Сохраните существующие bot/MCP-токены и путь к SQLite.
Перед первым запуском с **новым сервером Bot API** сбросьте только курсор
polling в сохранённой SQLite, пока poller остановлен:

```sql
UPDATE update_offset SET next_offset = 0 WHERE singleton = 1;
```

`update_id` относится к очереди конкретного сервера, поэтому облачный курсор
нельзя переносить в свежую локальную очередь. Обычные перезапуски того же
локального сервера сброса не требуют. Сделайте резервную копию базы;
привязки, журнал доставок и входящие сохраните. `Store.set_offset(0)` для этой
операции не подходит, поскольку обычное обновление курсора монотонно.

Для корректного порядка запуска после перезагрузки установите
`telegram-bot-api.service` в `~/.config/systemd/user/`, а `local-api.conf` — в
`~/.config/systemd/user/telegram-ping-mcp.service.d/local-api.conf`; выполните
`systemctl --user daemon-reload` и включите оба сервиса. Drop-in устанавливается
только при использовании локального API, облачный режим от него не зависит.
Затем запустите MCP, проверьте readiness и только после этого пробуйте файл
больше 20 МБ.
Локальный Bot API сохраняет скачанные файлы в собственном закрытом кэше;
учитывайте место на диске и персональные данные в резервных копиях.

Крупный файл выдаётся через защищённый HTTP-маршрут `/attachments/{id}`.
Он требует тот же Bearer-заголовок, что `/mcp`, и не содержит секрет в URL.
В MCP-контекст возвращаются только метаданные, чтобы файл на 100 МиБ не
превратился в огромную строку Base64 в контексте модели.
