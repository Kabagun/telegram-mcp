"""Date-bounded, stable history pages and metadata-only database/network isolation."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_ping_mcp.conversation import ConversationHistory
from telegram_ping_mcp.inbox import InboxStore
from telegram_ping_mcp.service import ServiceError
from telegram_ping_mcp.store import Store

DAY = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
START = "2024-01-01T00:00:00Z"
END = "2024-01-02T00:00:00Z"


class Reader:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.hook = None

    async def fetch_outgoing(self, chat_id, ids):
        self.calls.append((chat_id, ids.copy()))
        if self.hook is not None:
            self.hook()
        return {key: self.messages[key].copy() for key in ids if key in self.messages}

    async def fetch_recorded(self, ids):
        self.calls.append(("backfill", ids.copy()))
        if self.hook is not None:
            self.hook()
        return {key: self.messages[key].copy() for key in ids if key in self.messages}


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    for key, telegram_id in (("synthetic", 7001), ("other", 7002)):
        link = store.create_link(key, "Synthetic")
        store.consume_link(link["token"], telegram_id, telegram_id, None, "Synthetic")
    inbox = InboxStore(store.path, now=lambda: DAY + 100000)
    reader = Reader()
    return SimpleNamespace(
        store=store,
        inbox=inbox,
        reader=reader,
        user=store.get_user("synthetic"),
        other=store.get_user("other"),
        history=ConversationHistory(store, inbox, reader),
    )


def add_incoming(state, native_id, sent_at=DAY, *, user=None, chat_id=None, attachment=None):
    user = state.user if user is None else user
    state.inbox._save(
        user_key=user["user_key"],
        telegram_user_id=user["telegram_user_id"],
        chat_id=user["chat_id"] if chat_id is None else chat_id,
        telegram_message_id=native_id,
        topic_id=None,
        sent_at=sent_at,
        text="Synthetic incoming body",
        caption="Synthetic incoming caption",
        attachment=attachment,
    )
    return state.inbox.list_messages(user["user_key"], order="newest", limit=1)[0]["id"]


def add_outgoing(state, native_id, sent_at=DAY):
    state.inbox.record_outgoing(
        state.user, {"message_id": native_id, "chat": {"id": 7001}, "date": sent_at}
    )
    state.reader.messages[native_id] = {
        "telegram_message_id": native_id,
        "chat_id": 7001,
        "sent_at": sent_at,
        "text": "Synthetic outgoing body",
        "has_media": False,
    }
    return state.inbox.list_messages(direction="all", order="newest", limit=1)[0]["id"]


def guard_body_columns(inbox, monkeypatch):
    original = inbox._connection
    reads = []

    @contextmanager
    def guarded():
        with original() as connection:

            def authorize(action, table, column, _database, _source):
                if action == sqlite3.SQLITE_READ and table == "inbound_messages":
                    reads.append(column)
                    if column in {"text", "caption"}:
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            yield connection

    monkeypatch.setattr(inbox, "_connection", guarded)
    return reads


async def test_original_timestamp_bounds_apply_before_page_selection_and_exclude_nulls(state):
    add_incoming(state, 1, DAY - 1)
    lower = add_incoming(state, 2, DAY)
    middle = add_incoming(state, 3, DAY + 3600)
    add_incoming(state, 4, DAY + 86400)
    unknown = add_incoming(state, 5, None)
    # A late-indexed/backfilled message has an old original timestamp.
    add_outgoing(state, 6, DAY - 86400)
    first = await state.history.get_messages(state.user, since=START, until=END, limit=1)
    assert [item["id"] for item in first] == [middle]
    second = await state.history.get_messages(
        state.user, since=START, until=END, before_id=middle, limit=1
    )
    assert [item["id"] for item in second] == [lower]
    assert state.reader.calls == []
    unbounded = await state.history.get_messages(state.user, include_content=False)
    assert unknown in [item["id"] for item in unbounded]
    assert unknown not in [
        item["id"]
        for item in await state.history.get_messages(state.user, since=START, include_content=False)
    ]


async def test_equivalent_explicit_offsets_and_fractional_bounds_select_exact_seconds(state):
    add_incoming(state, 1, DAY)
    at_one = add_incoming(state, 2, DAY + 1)
    add_incoming(state, 3, DAY + 2)
    zero = await state.history.get_messages(state.user, since=START, until="2024-01-01T00:00:02Z")
    offset = await state.history.get_messages(
        state.user, since="2024-01-01T03:00:00+03:00", until="2023-12-31T19:00:02-05:00"
    )
    assert offset == zero
    fractional = await state.history.get_messages(
        state.user,
        since="2024-01-01T00:00:00.000001Z",
        until="2024-01-01T00:00:01.999999Z",
    )
    assert [item["id"] for item in fractional] == [at_one]
    # A valid sub-second interval can contain no integer-second Telegram event.
    assert (
        await state.history.get_messages(
            state.user, since="2024-01-01T00:00:00.1Z", until="2024-01-01T00:00:00.9Z"
        )
        == []
    )


async def test_default_page_is_ten_latest_ids_and_both_cursor_directions_are_stable(state):
    ids = [add_incoming(state, native_id, DAY + native_id) for native_id in range(1, 26)]
    latest = await state.history.get_messages(state.user)
    assert [item["id"] for item in latest] == list(reversed(ids[-10:]))
    older = await state.history.get_messages(
        state.user, before_id=min(item["id"] for item in latest)
    )
    assert [item["id"] for item in older] == list(reversed(ids[5:15]))
    oldest = await state.history.get_messages(state.user, order="oldest", limit=4)
    forward = await state.history.get_messages(
        state.user, order="oldest", after_id=max(item["id"] for item in oldest), limit=4
    )
    assert [item["id"] for item in oldest + forward] == ids[:8]
    narrow = await state.history.get_messages(state.user, after_id=ids[5], before_id=ids[10])
    assert [item["id"] for item in narrow] == list(reversed(ids[6:10]))
    assert len(state.inbox.list_messages()) == 25  # Legacy store defaults remain ascending/50.
    assert [item["id"] for item in state.inbox.list_messages()][:2] == ids[:2]


async def test_only_matching_selected_outgoing_ids_are_hydrated(state):
    add_incoming(state, 1, DAY + 20)
    first = add_outgoing(state, 20, DAY + 10)
    second = add_outgoing(state, 21, DAY + 30)
    add_outgoing(state, 22, DAY - 1)
    add_outgoing(state, 23, DAY + 86400)
    selected = await state.history.get_messages(state.user, since=START, until=END, limit=1)
    assert selected[0]["id"] == second
    assert state.reader.calls == [(7001, [21])]
    selected = await state.history.get_messages(
        state.user, since=START, until=END, before_id=second, direction="outgoing", limit=1
    )
    assert selected[0]["id"] == first
    assert state.reader.calls[-1] == (7001, [20])
    assert selected[0]["text"] == "Synthetic outgoing body"


async def test_metadata_list_and_single_never_read_body_columns_or_call_telegram(
    state, monkeypatch
):
    attachment = {
        "id": "a" * 32,
        "kind": "document",
        "name": "synthetic.pdf",
        "mime_type": "application/pdf",
        "size": 112000,
        "file_id": "synthetic-opaque-file",
    }
    incoming = add_incoming(state, 1, attachment=attachment)
    outgoing = add_outgoing(state, 20)
    state.store.reserve_delivery("legacy-not-yet-indexed", "a" * 64)
    state.store.finish_delivery("legacy-not-yet-indexed", "sent", message_id=30)
    reads = guard_body_columns(state.inbox, monkeypatch)
    items = await state.history.get_messages(state.user, include_content=False)
    assert {item["id"] for item in items} == {incoming, outgoing}
    for item in items:
        assert item["text"] is item["caption"] is None
        assert item["content_available"] is False
        assert item["content_source"] == "metadata"
        assert item == await state.history.get_messages(
            state.user, message_id=item["id"], include_content=False
        )
    assert (
        next(item for item in items if item["id"] == incoming)["attachment"]["name"]
        == "synthetic.pdf"
    )
    assert "text" not in reads and "caption" not in reads
    assert state.reader.calls == []
    assert state.store.get_history_backfill_cursor() == 0
    # Confirm the authorizer would reject a body read rather than merely observing it.
    with pytest.raises(sqlite3.DatabaseError):
        state.inbox.get_message(incoming, include_direction=True)


async def test_single_item_filters_use_metadata_before_loading_bodies(state, monkeypatch):
    item = add_incoming(state, 1)
    guard_body_columns(state.inbox, monkeypatch)
    assert await state.history.get_messages(state.user, message_id=item, until=START) is None
    assert await state.history.get_messages(state.user, message_id=item, after_id=item) is None
    assert await state.history.get_messages(state.user, message_id=item, before_id=item) is None
    assert (
        await state.history.get_messages(state.user, message_id=item, direction="outgoing") is None
    )
    assert state.reader.calls == []


@pytest.mark.parametrize("include_content", [True, False])
@pytest.mark.parametrize("foreign_kind", ["other-user", "group"])
async def test_single_foreign_identity_is_denied_before_date_cursor_or_direction_filter(
    state, monkeypatch, include_content, foreign_kind
):
    item = add_incoming(
        state,
        1,
        user=state.other if foreign_kind == "other-user" else state.user,
        chat_id=-100123 if foreign_kind == "group" else None,
    )
    guard_body_columns(state.inbox, monkeypatch)
    with pytest.raises(ServiceError, match="Access denied"):
        await state.history.get_messages(
            state.user,
            message_id=item,
            until=START,
            after_id=item,
            direction="outgoing",
            include_content=include_content,
        )
    assert state.reader.calls == []


@pytest.mark.parametrize(
    "options",
    [
        {"since": "2024-01-01"},
        {"since": "2024-01-01T00:00:00"},
        {"until": "2024-02-30T00:00:00Z"},
        {"since": "2024-01-01T00:00:00+24:00"},
        {"since": "2024-01-01T00:00:00+01:60"},
        {"until": "2024-01-01T00:00:00+00:99"},
        {"since": "2024-01-01T00:00:00-23:60"},
        {"since": "synthetic-private-input"},
        {"since": 1704067200},
        {"until": True},
        {"since": START, "until": START},
        {"since": END, "until": START},
        {"before_id": 0},
        {"before_id": True},
        {"before_id": 2**63},
        {"before_id": 5, "after_id": 5},
        {"before_id": 5, "after_id": 6},
        {"order": "timestamp"},
        {"order": []},
        {"include_content": "false"},
        {"include_content": 0},
        {"direction": []},
        {"limit": True},
    ],
)
async def test_invalid_query_arguments_are_fixed_errors_before_database_or_telegram(
    state, options, monkeypatch
):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid query reached persistence")

    monkeypatch.setattr(state.store, "get_user", unexpected)
    with pytest.raises(ServiceError) as caught:
        await state.history.get_messages(state.user, **options)
    assert "synthetic-private-input" not in str(caught.value)
    assert state.reader.calls == []


async def test_backfill_keeps_index_order_distinct_from_original_date_and_filters_hydration(state):
    recent = add_incoming(state, 1, DAY + 100)
    state.store.reserve_delivery("old-accepted-send", "a" * 64)
    state.store.finish_delivery("old-accepted-send", "sent", message_id=20)
    state.reader.messages[20] = {
        "telegram_message_id": 20,
        "chat_id": 7001,
        "sent_at": DAY - 100,
        "text": "Synthetic backfilled body",
        "has_media": False,
    }
    dated = await state.history.get_messages(state.user, since=START)
    assert [item["id"] for item in dated] == [recent]
    assert state.reader.calls == [("backfill", [20])]
    all_dates = await state.history.get_messages(state.user)
    assert all_dates[0]["id"] > recent
    assert all_dates[0]["sent_at"] < all_dates[1]["sent_at"]
    assert state.reader.calls[-1] == (7001, [20])


@pytest.mark.parametrize("phase", ["body", "backfill"])
async def test_binding_revoked_during_async_query_discards_selected_results(state, phase):
    if phase == "body":
        add_outgoing(state, 20)
    else:
        state.store.reserve_delivery("old-accepted-send", "a" * 64)
        state.store.finish_delivery("old-accepted-send", "sent", message_id=20)
    state.reader.hook = lambda: state.store.set_active_by_telegram(7001, False)
    with pytest.raises(ServiceError, match="Access denied"):
        await state.history.get_messages(state.user, since=START)


async def test_metadata_query_still_revalidates_binding_after_local_selection(state, monkeypatch):
    add_incoming(state, 1)
    original = state.inbox.list_messages

    def revoke_after_selection(*args, **kwargs):
        result = original(*args, **kwargs)
        state.store.set_active_by_telegram(7001, False)
        return result

    monkeypatch.setattr(state.inbox, "list_messages", revoke_after_selection)
    with pytest.raises(ServiceError, match="Access denied"):
        await state.history.get_messages(state.user, include_content=False)


async def test_empty_matching_window_does_not_fetch_any_bodies(state):
    add_outgoing(state, 20, DAY - 1)
    assert await state.history.get_messages(state.user, since=START) == []
    assert state.reader.calls == []
