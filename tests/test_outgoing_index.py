"""Outgoing indexing stores acknowledgements, never message bodies or URLs."""

import copy
import sqlite3
import traceback

import pytest

from telegram_ping_mcp.inbox import InboundController, InboxStore
from telegram_ping_mcp.service import ServiceError
from telegram_ping_mcp.store import Store

TELEGRAM_ID = 7001
USER_KEY = "synthetic"
FILE_ID = "synthetic-opaque-file-id"
TEXT = "Synthetic outgoing body that must never enter SQLite"
CAPTION = "Synthetic outgoing caption that must never enter SQLite"
QUOTED = "Synthetic quoted reply body that must never enter SQLite"
URL = "https://synthetic.invalid/private?signature=synthetic-secret"


def bind(store, key=USER_KEY, telegram_id=TELEGRAM_ID):
    link = store.create_link(key, "Synthetic")
    return store.consume_link(link["token"], telegram_id, telegram_id, None, "Synthetic")


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    user = bind(store)
    inbox = InboxStore(store.path, now=lambda: 1234.0)
    return store, inbox, user


def acknowledgement(message_id=61, **changes):
    message = {
        "message_id": message_id,
        "date": 1200,
        "chat": {"id": TELEGRAM_ID, "type": "private"},
        "text": TEXT,
        "caption": CAPTION,
        "reply_to_message": {"text": QUOTED, "caption": QUOTED},
        "link_preview_options": {"url": URL},
    }
    message.update(changes)
    return message


def document_ack(message_id=61, **changes):
    return acknowledgement(
        message_id,
        document={
            "file_id": FILE_ID,
            "file_name": "../synthetic.txt",
            "file_size": 19,
            "mime_type": "text/plain",
            "file_path": URL,
            "url": URL,
            **changes,
        },
    )


def save_incoming(inbox, message_id=31, *, user_key=USER_KEY, telegram_id=TELEGRAM_ID):
    inbox._save(
        user_key=user_key,
        telegram_user_id=telegram_id,
        chat_id=telegram_id,
        telegram_message_id=message_id,
        topic_id=None,
        sent_at=1100,
        text="Synthetic incoming text",
        caption=None,
        attachment=None,
    )


def test_additive_legacy_migration_preserves_existing_rows_and_id_sequence(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    user = bind(store)
    # The exact pre-direction schema, including an existing AUTOINCREMENT gap.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """CREATE TABLE inbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL CHECK (telegram_user_id > 0),
                chat_id INTEGER NOT NULL CHECK (chat_id != 0),
                telegram_message_id INTEGER NOT NULL CHECK (telegram_message_id > 0),
                topic_id INTEGER,
                sent_at INTEGER,
                received_at REAL NOT NULL,
                text TEXT,
                caption TEXT,
                attachment_id TEXT UNIQUE,
                attachment_kind TEXT,
                attachment_name TEXT,
                attachment_mime_type TEXT,
                attachment_size INTEGER,
                telegram_file_id TEXT,
                is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
                UNIQUE (chat_id, telegram_message_id)
            );
            CREATE INDEX custom_legacy_index ON inbound_messages (sent_at);"""
        )
        connection.execute(
            """INSERT INTO inbound_messages
               (id, user_key, telegram_user_id, chat_id, telegram_message_id,
                sent_at, received_at, text, caption, is_read)
               VALUES (12, ?, ?, ?, 31, 1100, 1101.5, 'Legacy incoming', 'Legacy caption', 1)""",
            (USER_KEY, TELEGRAM_ID, TELEGRAM_ID),
        )
    inbox = InboxStore(path)
    legacy = inbox.get_message(12)
    assert legacy["text"] == "Legacy incoming"
    assert legacy["caption"] == "Legacy caption"
    assert legacy["received_at"] == 1101.5
    assert legacy["read"] is True
    assert "direction" not in legacy
    assert inbox.get_message(12, include_direction=True)["direction"] == "incoming"
    inbox.record_outgoing(user, acknowledgement())
    save_incoming(inbox, 32)
    assert [row["id"] for row in inbox.list_messages(direction="all")] == [12, 13, 14]
    assert [row["id"] for row in inbox.list_messages(after_id=12)] == [14]
    assert InboxStore(path).get_message(12) == legacy
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'custom_legacy_index'"
        ).fetchone()


def test_legacy_and_mixed_pagination_share_stable_ids(setup):
    _, inbox, user = setup
    save_incoming(inbox, 31)
    inbox.record_outgoing(user, acknowledgement(61))
    save_incoming(inbox, 32)
    inbox.record_outgoing(user, acknowledgement(62))
    assert [item["id"] for item in inbox.list_messages()] == [1, 3]
    assert inbox.get_message(2) is None
    assert inbox.get_message(99) is None
    first = inbox.list_messages(direction="all", include_direction=True, limit=2)
    second = inbox.list_messages(
        direction="all", include_direction=True, after_id=first[-1]["id"], limit=2
    )
    assert [item["id"] for item in first + second] == [1, 2, 3, 4]
    assert [item["direction"] for item in first + second] == [
        "incoming",
        "outgoing",
        "incoming",
        "outgoing",
    ]
    assert first[0]["content_available"] is True
    assert first[0]["content_source"] == "stored"
    assert first[1]["content_available"] is False
    assert first[1]["content_source"] == "telegram"
    assert first[1]["text"] is first[1]["caption"] is None
    assert first[1]["read"] is True
    assert inbox.get_message(2, include_direction=True) == first[1]
    assert [item["id"] for item in inbox.list_messages(direction="outgoing")] == [2, 4]
    assert len(inbox.list_messages(user_key=USER_KEY, chat_id=TELEGRAM_ID, direction="all")) == 4
    assert inbox.list_messages(user_key="other", direction="all") == []
    assert inbox.list_messages(chat_id=TELEGRAM_ID + 1, direction="all") == []


@pytest.mark.parametrize("direction", [None, True, 1, [], {}, "both", "", "Incoming"])
def test_invalid_direction_is_a_fixed_error(setup, direction):
    with pytest.raises(ServiceError, match="Message direction must be"):
        setup[1].list_messages(direction=direction)


@pytest.mark.parametrize("value", [None, "true", 1, []])
def test_direction_inclusion_requires_a_boolean(setup, value):
    with pytest.raises(ServiceError):
        setup[1].list_messages(include_direction=value)
    with pytest.raises(ServiceError):
        setup[1].get_message(1, include_direction=value)


def test_idempotent_ack_never_overwrites_incoming_or_outgoing_rows(setup):
    _, inbox, user = setup
    save_incoming(inbox)
    inbox.record_outgoing(user, acknowledgement(31))
    assert inbox.get_message(1)["text"] == "Synthetic incoming text"
    inbox.record_outgoing(user, document_ack())
    original = inbox.list_messages(direction="outgoing", include_direction=True)
    inbox.record_outgoing(user, acknowledgement(date=9999, text="Different body"))
    assert inbox.list_messages(direction="outgoing", include_direction=True) == original
    assert len(inbox.list_messages(direction="all")) == 2


def test_attachment_metadata_roundtrip_without_any_outgoing_body_or_url_in_sql_or_wal(setup):
    store, inbox, user = setup
    # Keep a reader open so the WAL remains inspectable after indexing.
    with sqlite3.connect(store.path) as anchor:
        anchor.execute("SELECT count(*) FROM users").fetchone()
        inbox.record_outgoing(user, document_ack())
        item = inbox.list_messages(direction="outgoing", include_direction=True)[0]
        assert item["attachment"] == {
            "id": item["attachment"]["id"],
            "kind": "document",
            "name": "synthetic.txt",
            "mime_type": "text/plain",
            "size": 19,
        }
        assert item["sent_at"] == 1200
        assert item["received_at"] == 1234
        with sqlite3.connect(store.path) as connection:
            row = connection.execute("SELECT * FROM inbound_messages").fetchone()
            dump = "\n".join(connection.iterdump())
            assert connection.execute("SELECT text, caption FROM inbound_messages").fetchone() == (
                None,
                None,
            )
            assert FILE_ID in row
        files = list(store.path.parent.glob(store.path.name + "*"))
        assert any(file.name.endswith("-wal") for file in files)
        for secret in (TEXT, CAPTION, QUOTED, URL):
            assert secret not in dump
            assert all(secret.encode() not in file.read_bytes() for file in files)


@pytest.mark.parametrize(
    "filename", [URL, "synthetic.txt?signature=synthetic-secret", "file#secret"]
)
def test_url_like_attachment_filename_is_replaced_by_generic_metadata(setup, filename):
    _, inbox, user = setup
    inbox.record_outgoing(user, document_ack(file_name=filename))
    assert inbox.list_messages(direction="outgoing")[0]["attachment"]["name"] == "document"


def test_url_in_attachment_file_id_is_never_persisted(setup):
    _, inbox, user = setup
    with pytest.raises(ServiceError, match="Attachment identifier is invalid"):
        inbox.record_outgoing(user, document_ack(file_id=URL))
    assert inbox.list_messages(direction="all") == []


@pytest.mark.parametrize("transition", ["inactive", "unlinked", "replaced", "foreign_key"])
def test_stale_or_foreign_user_cannot_create_index_or_change_binding(setup, transition):
    store, inbox, old_user = setup
    if transition == "inactive":
        store.set_active_by_telegram(TELEGRAM_ID, False)
    elif transition == "unlinked":
        store.delete_user(USER_KEY)
    elif transition == "replaced":
        store.delete_user(USER_KEY)
        bind(store, telegram_id=TELEGRAM_ID + 1)
    else:
        old_user = {**old_user, "user_key": "foreign"}
    before = store.list_users()
    inbox.record_outgoing(old_user, acknowledgement())
    assert inbox.list_messages(direction="all") == []
    assert store.list_users() == before


def test_unlink_and_reenroll_never_reassigns_existing_history(setup):
    store, inbox, user = setup
    inbox.record_outgoing(user, acknowledgement())
    original = inbox.list_messages(direction="outgoing", include_direction=True)[0]
    store.delete_user(USER_KEY)
    replacement = bind(store, key="replacement")
    inbox.record_outgoing(replacement, acknowledgement())
    assert inbox.list_messages(user_key="replacement", direction="all") == []
    assert inbox.get_message(original["id"], include_direction=True) == original
    assert store.get_user("replacement")["active"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"chat": None},
        {"chat": {"id": TELEGRAM_ID + 1}},
        {"chat": {"id": -100123, "type": "supergroup"}},
        {"chat": {"id": TELEGRAM_ID, "type": "group"}},
        {"chat": {"id": True}},
        {"message_thread_id": 9},
        {"message_id": True},
        {"message_id": 0},
        {"message_id": 2**31},
        {"date": True},
        {"date": -1},
    ],
)
def test_mismatched_or_invalid_acknowledgements_are_rejected(setup, changes):
    _, inbox, user = setup
    with pytest.raises(ServiceError):
        inbox.record_outgoing(user, acknowledgement(**changes))
    assert inbox.list_messages(direction="all") == []


def test_group_user_binding_and_malformed_inputs_are_rejected(setup):
    _, inbox, user = setup
    for invalid_user, ack in [
        ({**user, "chat_id": -100123}, acknowledgement()),
        ({**user, "telegram_user_id": True}, acknowledgement()),
        (None, acknowledgement()),
        (user, None),
    ]:
        with pytest.raises(ServiceError):
            inbox.record_outgoing(invalid_user, ack)
    assert inbox.list_messages(direction="all") == []


def test_missing_ack_date_uses_clock_without_copying_body(setup):
    _, inbox, user = setup
    ack = acknowledgement()
    del ack["date"]
    inbox.record_outgoing(user, ack)
    assert inbox.list_messages(direction="outgoing")[0]["sent_at"] == 1234


def test_sqlite_index_failures_have_no_unsafe_exception_chain(setup, monkeypatch):
    _, inbox, user = setup

    def fail():
        raise sqlite3.OperationalError(URL)

    monkeypatch.setattr(inbox, "_connection", fail)
    with pytest.raises(ServiceError) as caught:
        inbox.record_outgoing(user, acknowledgement())
    assert str(caught.value) == "Outgoing message metadata could not be indexed."
    assert caught.value.__context__ is None
    assert URL not in "".join(traceback.format_exception(caught.value))


def test_sent_delivery_id_backfill_is_distinct_bounded_sorted_and_release_free(setup):
    store, _, _ = setup
    records = [
        ("normal-1", "sent", 10),
        ("normal-2", "sent", 5),
        ("duplicate-id", "sent", 10),
        ("normal-last", "sent", 2**31 - 1),
        ("release:synthetic", "sent", 6),
        ("release-message:synthetic", "sent", 7),
        ("failed", "failed", 8),
        ("unknown", "unknown", 9),
        ("sending", "sending", 11),
        ("missing", "sent", None),
        ("oversized", "sent", 2**31),
        ("zero", "sent", 0),
        ("negative", "sent", -1),
        ("text", "sent", "malformed"),
    ]
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            "INSERT INTO deliveries(request_id, fingerprint, status, message_id) "
            "VALUES (?, ?, ?, ?)",
            [(request, "a" * 64, status, message_id) for request, status, message_id in records],
        )
    assert store.list_sent_delivery_ids() == [5, 10, 2**31 - 1]
    assert store.list_sent_delivery_ids(limit=1) == [5]
    assert store.list_sent_delivery_ids(after_message_id=5, limit=1) == [10]
    assert store.list_sent_delivery_ids(after_message_id=2**31 - 1) == []


@pytest.mark.parametrize("after", [True, -1, 2**31, "1", None])
def test_sent_id_cursor_validation(setup, after):
    with pytest.raises(ValueError):
        setup[0].list_sent_delivery_ids(after_message_id=after)


@pytest.mark.parametrize("limit", [True, 0, -1, 101, "1", None])
def test_sent_id_limit_validation(setup, limit):
    with pytest.raises(ValueError):
        setup[0].list_sent_delivery_ids(limit=limit)


def test_backfill_cursor_is_durable_monotonic_and_metadata_only(setup):
    store, _, _ = setup
    assert store.get_history_backfill_cursor() == 0
    for value in (30, 12, 30, 0, 2**31 - 1):
        store.set_history_backfill_cursor(value)
    assert store.get_history_backfill_cursor() == 2**31 - 1
    assert Store(store.path).get_history_backfill_cursor() == 2**31 - 1
    with sqlite3.connect(store.path) as connection:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(history_backfill_state)")
        ]
        assert columns == ["singleton", "last_message_id"]
        assert connection.execute("SELECT * FROM history_backfill_state").fetchall() == [
            (1, 2**31 - 1)
        ]


@pytest.mark.parametrize("value", [True, -1, 2**31, "1", None])
def test_backfill_cursor_validation_does_not_advance(setup, value):
    store, _, _ = setup
    store.set_history_backfill_cursor(15)
    with pytest.raises(ValueError):
        store.set_history_backfill_cursor(value)
    assert store.get_history_backfill_cursor() == 15


class ReplyBot:
    def __init__(self):
        self.calls = 0
        self.hook = None

    async def send_attachment(self, chat_id, kind, file_id, **options):
        self.calls += 1
        if self.hook:
            self.hook()
        result = document_ack()
        result["chat"] = {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}
        return result


async def file_reply_setup(setup, *, group=False):
    store, inbox, _ = setup
    bot = ReplyBot()
    controller = InboundController(store, inbox, bot, allowed_group_chat_ids=frozenset({-100123}))
    chat_id = -100123 if group else TELEGRAM_ID
    incoming = {
        "message_id": 31,
        "from": {"id": TELEGRAM_ID, "is_bot": False},
        "chat": {"id": chat_id, "type": "supergroup" if group else "private"},
        "document": {"file_id": FILE_ID, "file_size": 19},
    }
    await controller.handle_update({"message": incoming})
    return controller, bot, inbox.list_messages()[0]["id"]


async def test_private_file_reply_indexes_before_finishing_and_duplicate_never_resends(
    setup, monkeypatch
):
    store, inbox, _ = setup
    controller, bot, message_id = await file_reply_setup(setup)
    finish = store.finish_delivery

    def assert_indexed_before_finish(request_id, status, **options):
        if status == "sent":
            assert inbox.list_messages(direction="outgoing")[0]["telegram_message_id"] == 61
        finish(request_id, status, **options)

    monkeypatch.setattr(store, "finish_delivery", assert_indexed_before_finish)
    first = await controller.reply_attachment(message_id, "Synthetic caption", "reply-index")
    second = await controller.reply_attachment(message_id, "Synthetic caption", "reply-index")
    assert first["status"] == second["status"] == "sent"
    assert second["duplicate"] is True
    assert bot.calls == 1
    assert len(inbox.list_messages(direction="outgoing")) == 1


async def test_group_file_reply_does_not_create_private_history(setup):
    _, inbox, _ = setup
    controller, bot, message_id = await file_reply_setup(setup, group=True)
    assert (await controller.reply_attachment(message_id, "", "reply-group"))["status"] == "sent"
    assert bot.calls == 1
    assert inbox.list_messages(direction="outgoing") == []


async def test_index_failure_logs_only_static_warning_and_preserves_sent_status(
    setup, monkeypatch, caplog
):
    store, inbox, _ = setup
    controller, bot, message_id = await file_reply_setup(setup)

    def fail(*args):
        raise ServiceError(TEXT + URL)

    monkeypatch.setattr(inbox, "record_outgoing", fail)
    result = await controller.reply_attachment(message_id, "", "reply-index-error")
    assert result["status"] == "sent"
    assert store.list_sent_delivery_ids() == [61]
    assert bot.calls == 1
    assert [record.getMessage() for record in caplog.records] == [
        "Outgoing message metadata could not be indexed."
    ]
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("transition", ["inactive", "unlinked", "rebound"])
async def test_binding_change_during_successful_send_does_not_index_or_reactivate(
    setup, transition
):
    store, inbox, _ = setup
    controller, bot, message_id = await file_reply_setup(setup)

    def change_binding():
        if transition == "inactive":
            store.set_active_by_telegram(TELEGRAM_ID, False)
        else:
            store.delete_user(USER_KEY)
            if transition == "rebound":
                bind(store, telegram_id=TELEGRAM_ID + 1)

    bot.hook = change_binding
    assert (await controller.reply_attachment(message_id, "", "reply-transition"))[
        "status"
    ] == "sent"
    assert inbox.list_messages(direction="outgoing") == []
    assert store.list_sent_delivery_ids() == [61]
    if transition == "inactive":
        assert store.get_user(USER_KEY)["active"] is False
    elif transition == "unlinked":
        assert store.get_user(USER_KEY) is None
    else:
        assert store.get_user(USER_KEY)["telegram_user_id"] == TELEGRAM_ID + 1


def test_outgoing_metadata_projection_does_not_mutate_acknowledgement(setup):
    _, inbox, user = setup
    ack = document_ack(file_name=URL)
    original = copy.deepcopy(ack)
    inbox.record_outgoing(user, ack)
    assert ack == original
