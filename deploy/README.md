# systemd user deployment

The service runs as `apps` from `/srv/apps/telegram-ping-mcp/app` and listens on
`127.0.0.1:18004`. Confirm that the port is available before installation. Keep
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
configuration and leaves the service stopped. No root commands are needed.

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
curl --fail --silent http://127.0.0.1:18004/health
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
