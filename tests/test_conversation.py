"""Private mixed-history reads with live Telegram bodies and no body persistence."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from telegram_ping_mcp.conversation import ConversationHistory
from telegram_ping_mcp.inbox import InboxStore
from telegram_ping_mcp.service import ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram_history import HistoryUnavailable


class Reader:
    def __init__(self):
        self.messages = {}
        self.calls = []
        self.failure = False
        self.entered = None
        self.proceed = None

    async def _fetch(self, ids):
        if self.failure:
            raise HistoryUnavailable()
        if self.entered is not None:
            self.entered.set()
            await self.proceed.wait()
        return {key: self.messages[key].copy() for key in ids if key in self.messages}

    async def fetch_recorded(self, ids):
        self.calls.append(("backfill", ids))
        return await self._fetch(ids)

    async def fetch_outgoing(self, chat_id, ids):
        self.calls.append((chat_id, ids))
        result = await self._fetch(ids)
        assert all(value["chat_id"] == chat_id for value in result.values())
        return result


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "private.sqlite3"
    store, inbox = Store(path), InboxStore(path)
    users = []
    for key, telegram_id in (("alpha", 7001), ("beta", 7002)):
        link = store.create_link(key, "Synthetic")
        store.consume_link(link["token"], telegram_id, telegram_id, None, "Synthetic")
        users.append(store.get_user(key))
    reader = Reader()
    return SimpleNamespace(
        path=path,
        store=store,
        inbox=inbox,
        reader=reader,
        users=users,
        history=ConversationHistory(store, inbox, reader),
    )


def incoming(state, user, native_id=10):
    state.inbox._save(
        user_key=user["user_key"],
        telegram_user_id=user["telegram_user_id"],
        chat_id=user["chat_id"],
        telegram_message_id=native_id,
        topic_id=None,
        sent_at=100,
        text="Synthetic incoming",
        caption=None,
        attachment=None,
    )


def outgoing(state, user, native_id=20, media=False):
    state.inbox.record_outgoing(
        user,
        {
            "message_id": native_id,
            "chat": {"id": user["chat_id"]},
            "date": 200,
            "text": "DO_NOT_PERSIST_SYNTHETIC_OUTGOING",
        },
    )
    state.reader.messages[native_id] = {
        "telegram_message_id": native_id,
        "chat_id": user["chat_id"],
        "sent_at": 200,
        "text": "DO_NOT_PERSIST_SYNTHETIC_OUTGOING",
        "has_media": media,
    }


async def test_mixed_history_hydrates_each_read_and_preserves_cursor(state):
    user = state.users[0]
    incoming(state, user)
    outgoing(state, user)
    outgoing(state, state.users[1], 30)
    first = await state.history.get_messages(user, limit=1, order="oldest")
    assert len(first) == 1 and first[0]["direction"] == "incoming"
    second = await state.history.get_messages(
        user, after_id=first[0]["id"], limit=1, order="oldest"
    )
    assert second[0]["direction"] == "outgoing"
    assert second[0]["text"] == "DO_NOT_PERSIST_SYNTHETIC_OUTGOING"
    assert second[0]["content_source"] == "telegram" and second[0]["content_available"]
    state.reader.messages[20]["text"] = "Synthetic edited body"
    single = await state.history.get_messages(user, message_id=second[0]["id"])
    assert single["text"] == "Synthetic edited body"
    assert state.reader.calls == [(7001, [20]), (7001, [20])]
    with sqlite3.connect(state.path) as connection:
        persisted = connection.execute(
            "SELECT text,caption FROM inbound_messages WHERE direction='outgoing'"
        ).fetchall()
        assert persisted == [(None, None), (None, None)]
        assert "DO_NOT_PERSIST_SYNTHETIC_OUTGOING" not in "\n".join(connection.iterdump())


async def test_caption_missing_body_and_disabled_reader_are_explicit(state):
    user = state.users[0]
    outgoing(state, user, media=True)
    item = (await state.history.get_messages(user))[0]
    assert item["text"] is None and item["caption"] and item["content_available"]
    state.reader.messages.clear()
    missing = (await state.history.get_messages(user))[0]
    assert missing["text"] is None and missing["caption"] is None
    assert missing["content_available"] is False
    disabled = await ConversationHistory(state.store, state.inbox).get_messages(user)
    assert disabled == [missing]


async def test_direction_incoming_does_not_contact_telegram_even_when_unavailable(state):
    user = state.users[0]
    incoming(state, user)
    outgoing(state, user)
    state.reader.failure = True
    assert len(await state.history.get_messages(user, direction="incoming")) == 1
    assert state.reader.calls == []
    with pytest.raises(HistoryUnavailable):
        await state.history.get_messages(user)


@pytest.mark.parametrize("direction", ["both", "", "ALL", None, 1])
async def test_invalid_direction_does_not_fetch(state, direction):
    with pytest.raises(ServiceError, match="Direction"):
        await state.history.get_messages(state.users[0], direction=direction)
    assert state.reader.calls == []


async def test_single_foreign_or_group_item_is_denied_before_body_fetch(state):
    outgoing(state, state.users[1])
    foreign = state.inbox.list_messages(direction="all", include_direction=True)[0]
    with pytest.raises(ServiceError, match="Access denied"):
        await state.history.get_messages(state.users[0], message_id=foreign["id"])
    assert state.reader.calls == []
    assert await state.history.get_messages(state.users[0], message_id=999) is None


@pytest.mark.parametrize(
    "options",
    [
        {"after_id": -1},
        {"after_id": True},
        {"limit": 0},
        {"limit": 101},
        {"message_id": True},
        {"message_id": 0},
    ],
)
async def test_invalid_cursor_is_rejected_before_telegram(state, options):
    with pytest.raises(ServiceError):
        await state.history.get_messages(state.users[0], **options)
    assert state.reader.calls == []


@pytest.mark.parametrize("operation", ["deactivate", "delete", "rebind"])
async def test_binding_revocation_during_telegram_request_discards_body(state, operation):
    user = state.users[0]
    outgoing(state, user)
    state.reader.entered, state.reader.proceed = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(state.history.get_messages(user))
    await state.reader.entered.wait()
    if operation == "deactivate":
        state.store.set_active_by_telegram(7001, False)
    else:
        state.store.delete_user("alpha")
        if operation == "rebind":
            link = state.store.create_link("alpha", "Synthetic replacement")
            state.store.consume_link(link["token"], 7003, 7003, None, "Synthetic")
    state.reader.proceed.set()
    with pytest.raises(ServiceError, match="Access denied"):
        await task


async def test_backfill_uses_known_successful_ids_only_and_stores_no_body(state):
    user = state.users[0]
    outgoing(state, user)
    # Remove the synthetic index to emulate a delivery predating this feature.
    with sqlite3.connect(state.path) as connection:
        connection.execute("DELETE FROM inbound_messages")
    for request_id, native_id, status in (
        ("old-success", 20, "sent"),
        ("old-unknown", 21, "unknown"),
        ("release:v1:alpha", 22, "sent"),
        ("old-failed", 23, "failed"),
    ):
        state.store.reserve_delivery(request_id, "a" * 64)
        state.store.finish_delivery(request_id, status, message_id=native_id)
    items = await state.history.get_messages(user)
    assert len(items) == 1 and items[0]["text"]
    assert state.reader.calls == [("backfill", [20]), (7001, [20])]
    assert state.store.get_history_backfill_cursor() == 20
    await state.history.get_messages(user)
    assert state.reader.calls[-1] == (7001, [20])
    assert sum(call[0] == "backfill" for call in state.reader.calls) == 1
    with sqlite3.connect(state.path) as connection:
        assert "DO_NOT_PERSIST_SYNTHETIC_OUTGOING" not in "\n".join(connection.iterdump())


async def test_backfill_failure_does_not_advance_cursor(state):
    state.store.reserve_delivery("synthetic", "a" * 64)
    state.store.finish_delivery("synthetic", "sent", message_id=20)
    state.reader.failure = True
    with pytest.raises(HistoryUnavailable):
        await state.history.get_messages(state.users[0])
    assert state.store.get_history_backfill_cursor() == 0


async def test_outgoing_single_direction_filter(state):
    incoming(state, state.users[0])
    item = state.inbox.list_messages()[0]
    assert (
        await state.history.get_messages(
            state.users[0], message_id=item["id"], direction="outgoing"
        )
        is None
    )
