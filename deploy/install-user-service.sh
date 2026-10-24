#!/bin/sh
# Install the service as apps. Credentials and activation are separate steps.
set -eu
umask 077

if [ "$(id -un)" != apps ]; then
    printf '%s\n' 'Run this installer as the apps user.' >&2
    exit 1
fi

app=/srv/apps/telegram-ping-mcp/app
if [ "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)" != "$app" ]; then
    printf '%s\n' "Place the application at $app before installing." >&2
    exit 1
fi

command -v systemctl >/dev/null
uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ]; then
    for candidate in "$HOME/.local/bin/uv" "$HOME/Programs/uv/uv"; do
        if [ -x "$candidate" ]; then
            uv_bin=$candidate
            break
        fi
    done
fi
if [ -z "$uv_bin" ]; then
    printf '%s\n' 'uv is required; install it under the apps user first.' >&2
    exit 1
fi

cd "$app"
"$uv_bin" sync --frozen --no-dev --no-editable
install -d -m 700 "$HOME/.config/telegram-ping-mcp" \
    "$HOME/.local/share/telegram-ping-mcp" "$HOME/.config/systemd/user"
if [ ! -e "$HOME/.config/telegram-ping-mcp/server.env" ]; then
    install -m 600 deploy/server.env.example "$HOME/.config/telegram-ping-mcp/server.env"
fi
install -m 600 deploy/telegram-ping-mcp.service \
    "$HOME/.config/systemd/user/telegram-ping-mcp.service"
systemctl --user daemon-reload
printf '%s\n' 'Installed. Add protected credentials, validate, then enable and start the user service.'
