"""Telegram Ping MCP command-line entry point."""

import argparse
import asyncio
import json
import logging
import sys
from importlib.metadata import version as installed_version
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .broadcast import BroadcastError, broadcast_release
from .config import Config, ConfigError
from .server import Runtime, StartupError, create_http_app, create_mcp
from .store import Store
from .telegram import TelegramAPIError, TelegramClient, TelegramTransportError


async def _stdio(runtime: Runtime) -> None:
    async with runtime.lifespan():
        await create_mcp(runtime).run_stdio_async()


def _read_release_message(message_file: str) -> str:
    with Path(message_file).open(encoding="utf-8") as source:
        return source.read(4097)


async def _broadcast(config: Config, message_file: str, *, retry_failed: bool) -> dict[str, int]:
    if not config.bot_token:
        raise BroadcastError("Telegram bot configuration is required.")
    message = await asyncio.to_thread(_read_release_message, message_file)
    release_version = installed_version("telegram-ping-mcp")
    store = Store(config.database_path)
    bot = TelegramClient(
        config.bot_token,
        api_base_url=config.api_base_url,
        local_files_root=config.local_files_root,
    )
    try:
        return await broadcast_release(
            store, bot, version=release_version, message=message, retry_failed=retry_failed
        )
    finally:
        await bot.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot-only Telegram Ping MCP")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Show offline configuration diagnostics without secrets")
    broadcast = commands.add_parser(
        "broadcast-release", help="Send an approved silent release notification once per version"
    )
    broadcast.add_argument("--message-file", required=True)
    broadcast.add_argument("--retry-failed", action="store_true")
    serve = commands.add_parser("serve", help="Start the bot and MCP transport")
    serve.add_argument("--transport", choices=("http", "stdio"), default="http")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    # HTTPX request logs contain bot-token-bearing URLs. Never enable them here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv(override=False)
    try:
        config = Config.from_env()
        if args.command == "doctor":
            print(json.dumps(config.diagnostic(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "broadcast-release":
            try:
                result = asyncio.run(
                    _broadcast(config, args.message_file, retry_failed=args.retry_failed)
                )
            except (Exception, asyncio.CancelledError):
                print("Release broadcast failed.", file=sys.stderr)
                return 1
            print(json.dumps(result))
            return 0
        config.validate_for_serve(args.transport, args.host)
        if not 1 <= args.port <= 65535:
            raise ConfigError("Port must be between 1 and 65535.")
        runtime = Runtime(config)
        if args.transport == "stdio":
            asyncio.run(_stdio(runtime))
        else:
            uvicorn.run(create_http_app(runtime), host=args.host, port=args.port, access_log=False)
        return 0
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except (StartupError, TelegramAPIError, TelegramTransportError):
        print(
            "Telegram startup failed. Check bot configuration and polling ownership.",
            file=sys.stderr,
        )
        return 1
    except OSError:
        print("Cannot access the configured database or network endpoint.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
