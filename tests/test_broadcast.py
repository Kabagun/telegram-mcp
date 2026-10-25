import asyncio
import sqlite3

import pytest

from telegram_ping_mcp import broadcast
from telegram_ping_mcp.broadcast import BroadcastError, broadcast_release
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

MESSAGE = "Синтетическое обновление\n\n• Новая возможность."
VERSION = "1.2.3"


class FakeBot:
    def __init__(self, outcome=None):
        self.calls = []
        self.command_updates = []
        self.outcome = outcome

    async def set_commands(self, commands, *, chat_id=None):
        self.command_updates.append((commands, chat_id))

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is not None:
            return self.outcome
        return {"message_id": len(self.calls), "chat": {"id": chat_id}}


def bind(store, key="synthetic", telegram_id=7001):
    link = store.create_link(key, key)
    return store.consume_link(link["token"], telegram_id, telegram_id, None, "Synthetic")


def recipient_deliveries(store):
    with sqlite3.connect(store.path) as database:
        return database.execute(
            "SELECT request_id, fingerprint, status, message_id, error FROM deliveries "
            "WHERE request_id LIKE 'release:%' ORDER BY request_id"
        ).fetchall()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(broadcast, "_SEND_INTERVAL_SECONDS", 0)
    return Store(tmp_path / "synthetic-broadcast.sqlite3")


async def test_release_is_silent_once_per_version_and_never_stores_outgoing_text(store):
    bind(store)
    bind(store, "synthetic-second", 7002)
    bot = FakeBot()

    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 2,
        "failed": 0,
    }
    assert bot.calls == [
        {"chat_id": 7001, "text": MESSAGE, "disable_notification": True},
        {"chat_id": 7002, "text": MESSAGE, "disable_notification": True},
    ]
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 0,
    }
    assert len(bot.calls) == 2
    assert bot.command_updates == [([], 7001), ([], 7002)]
    assert await broadcast_release(store, bot, version="1.2.4", message=MESSAGE) == {
        "sent": 2,
        "failed": 0,
    }
    with sqlite3.connect(store.path) as database:
        dump = "\n".join(database.iterdump())
    assert MESSAGE not in dump
    assert "Новая возможность" not in dump
    assert len(recipient_deliveries(store)) == 4


async def test_release_preserves_the_current_interaction_keyboard(store):
    bind(store)
    bot = FakeBot()
    keyboard = {"keyboard": [[{"text": "Отмена"}]], "resize_keyboard": True}
    result = await broadcast_release(
        store,
        bot,
        version=VERSION,
        message=MESSAGE,
        reply_markup_for=lambda chat_id: keyboard,
    )
    assert result == {"sent": 1, "failed": 0}
    assert bot.calls[0]["reply_markup"] == keyboard


async def test_menu_failure_is_retryable_without_a_message_attempt(store):
    bind(store)

    class MenuFailure(FakeBot):
        async def set_commands(self, commands, *, chat_id=None):
            raise TelegramTransportError()

    failed = MenuFailure()
    assert await broadcast_release(store, failed, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 1,
    }
    assert failed.calls == []
    healthy = FakeBot()
    assert await broadcast_release(
        store, healthy, version=VERSION, message=MESSAGE, retry_failed=True
    ) == {"sent": 1, "failed": 0}


async def test_changed_message_for_same_version_fails_before_new_recipients(store):
    bind(store)
    bot = FakeBot()
    await broadcast_release(store, bot, version=VERSION, message=MESSAGE)
    bind(store, "a-synthetic-new", 7002)

    with pytest.raises(BroadcastError, match="differs from the reserved version"):
        await broadcast_release(store, bot, version=VERSION, message="Changed synthetic message")

    assert len(bot.calls) == 1
    assert len(recipient_deliveries(store)) == 1


async def test_guard_does_not_count_as_a_sent_notification_when_no_users_are_eligible(store):
    bot = FakeBot()
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 0,
    }
    assert bot.calls == []


async def test_inflight_delivery_is_not_reclaimed_by_concurrent_retry(store):
    bind(store)
    started = asyncio.Event()
    release = asyncio.Event()

    class WaitingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            started.set()
            await release.wait()
            return await super().send_message(chat_id, text, **kwargs)

    bot = WaitingBot()
    task = asyncio.create_task(broadcast_release(store, bot, version=VERSION, message=MESSAGE))
    await started.wait()
    try:
        assert await broadcast_release(
            store, bot, version=VERSION, message=MESSAGE, retry_failed=True
        ) == {"sent": 0, "failed": 0}
    finally:
        release.set()
    assert await task == {"sent": 1, "failed": 0}
    assert len(bot.calls) == 1


async def test_snapshot_filters_inactive_and_nonprivate_or_malformed_users(store, monkeypatch):
    user = bind(store)
    bind(store, "synthetic-inactive", 7002)
    store.set_active_by_telegram(7002, False)
    original_list = store.list_users
    invalid = [
        {**user, "chat_id": -7001},
        {**user, "chat_id": 7002},
        {**user, "telegram_user_id": True, "chat_id": True},
        {**user, "active": 1},
        {**user, "user_key": ""},
        None,
    ]
    monkeypatch.setattr(
        store, "list_users", lambda query, limit: original_list(query, limit) + invalid
    )
    bot = FakeBot()

    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 1,
        "failed": 0,
    }
    assert [call["chat_id"] for call in bot.calls] == [7001]


@pytest.mark.parametrize("change", ["delete", "replace", "inactive"])
async def test_binding_is_checked_after_snapshot_before_reservation(store, monkeypatch, change):
    bind(store)
    original_list = store.list_users

    def snapshot(query, limit):
        users = original_list(query, limit)
        if change == "inactive":
            store.set_active_by_telegram(7001, False)
        else:
            store.delete_user("synthetic")
            if change == "replace":
                bind(store, "synthetic", 7002)
        return users

    monkeypatch.setattr(store, "list_users", snapshot)
    bot = FakeBot()
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 0,
    }
    assert bot.calls == []
    assert recipient_deliveries(store) == []


async def test_binding_is_verified_again_after_claim_before_send(store, monkeypatch):
    bind(store)
    original_reserve = store.reserve_delivery

    def reserve(request_id, fingerprint, **kwargs):
        result = original_reserve(request_id, fingerprint, **kwargs)
        if request_id.startswith("release:"):
            store.delete_user("synthetic")
        return result

    monkeypatch.setattr(store, "reserve_delivery", reserve)
    bot = FakeBot()
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 1,
    }
    assert bot.calls == []
    assert recipient_deliveries(store)[0][2] == "failed"


async def test_only_explicit_retry_reclaims_known_failures(store):
    bind(store)
    bot = FakeBot(TelegramAPIError(429, 1))
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 1,
    }
    bot.outcome = None
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 0,
    }
    assert len(bot.calls) == 1
    assert await broadcast_release(
        store, bot, version=VERSION, message=MESSAGE, retry_failed=True
    ) == {"sent": 1, "failed": 0}
    assert len(bot.calls) == 2
    assert recipient_deliveries(store)[0][2] == "sent"


@pytest.mark.parametrize(
    "outcome",
    [
        TelegramTransportError(),
        OSError("synthetic-private-error"),
        {},
        {"message_id": True, "chat": {"id": 7001}},
        {"message_id": 1, "chat": {"id": 7002}},
    ],
)
async def test_unknown_outcomes_are_never_retried_and_do_not_log_data(store, caplog, outcome):
    bind(store)
    bot = FakeBot(outcome)
    assert await broadcast_release(store, bot, version=VERSION, message=MESSAGE) == {
        "sent": 0,
        "failed": 1,
    }
    bot.outcome = None
    assert await broadcast_release(
        store, bot, version=VERSION, message=MESSAGE, retry_failed=True
    ) == {"sent": 0, "failed": 0}
    assert len(bot.calls) == 1
    assert recipient_deliveries(store)[0][2] == "unknown"
    assert MESSAGE not in caplog.text
    assert "synthetic-private-error" not in caplog.text


async def test_cancelled_send_is_unknown_and_is_not_reclaimed(store):
    bind(store)
    bot = FakeBot(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await broadcast_release(store, bot, version=VERSION, message=MESSAGE)
    assert recipient_deliveries(store)[0][2] == "unknown"
    bot.outcome = None
    assert await broadcast_release(
        store, bot, version=VERSION, message=MESSAGE, retry_failed=True
    ) == {"sent": 0, "failed": 0}
    assert len(bot.calls) == 1


async def test_forbidden_response_marks_original_binding_inactive(store):
    bind(store)
    bot = FakeBot(TelegramAPIError(403))
    await broadcast_release(store, bot, version=VERSION, message=MESSAGE)
    assert store.get_user("synthetic")["active"] is False


async def test_forbidden_response_cannot_deactivate_replacement_binding(store):
    bind(store)

    class ReplacingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            store.delete_user("synthetic")
            bind(store, "synthetic-replacement", 7001)
            raise TelegramAPIError(403)

    await broadcast_release(store, ReplacingBot(), version=VERSION, message=MESSAGE)
    assert store.get_user("synthetic-replacement")["active"] is True


@pytest.mark.parametrize(
    ("version", "message", "retry_failed"),
    [
        ("", MESSAGE, False),
        ("synthetic\nversion", MESSAGE, False),
        (VERSION, "", False),
        (VERSION, "🙂" * 2049, False),
        (VERSION, "synthetic\x00message", False),
        (VERSION, "synthetic\ud800message", False),
        (VERSION, MESSAGE, 1),
    ],
)
async def test_invalid_input_is_rejected_before_any_delivery_records(
    store, version, message, retry_failed
):
    bot = FakeBot()
    with pytest.raises(BroadcastError):
        await broadcast_release(
            store, bot, version=version, message=message, retry_failed=retry_failed
        )
    assert bot.calls == []
    with sqlite3.connect(store.path) as database:
        assert database.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
