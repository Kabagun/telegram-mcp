"""Validated history windows over original integer timestamps and stable index IDs."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .service import ServiceError, _integer

_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DATE_ERROR = "History dates must be ISO 8601 timestamps with an explicit timezone."


def _microseconds(value: str | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64 or _TIMESTAMP.fullmatch(value) is None:
        raise ServiceError(_DATE_ERROR)
    parsed = None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value[-1] in "Zz" else value)
    except ValueError:
        pass
    if parsed is None:
        raise ServiceError(_DATE_ERROR)
    delta = parsed - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    after_id: int
    before_id: int | None
    limit: int
    order: str
    since: int | None
    until: int | None
    include_content: bool

    @classmethod
    def parse(
        cls,
        *,
        after_id: int = 0,
        before_id: int | None = None,
        limit: int = 10,
        order: str = "newest",
        since: str | None = None,
        until: str | None = None,
        include_content: bool = True,
    ) -> "HistoryQuery":
        _integer(after_id, minimum=0)
        _integer(limit, maximum=100)
        if before_id is not None:
            _integer(before_id)
            if after_id >= before_id:
                raise ServiceError("after_id must be less than before_id.")
        if not isinstance(order, str) or order not in {"newest", "oldest"}:
            raise ServiceError("Order must be newest or oldest.")
        if not isinstance(include_content, bool):
            raise ServiceError("include_content must be a boolean.")
        lower, upper = _microseconds(since), _microseconds(until)
        if lower is not None and upper is not None and lower >= upper:
            raise ServiceError("since must be earlier than until.")
        # Telegram sent_at is an integer Unix second. Ceil both precise bounds:
        # t >= since and t < until remain exact even for fractional timestamps.
        return cls(
            after_id,
            before_id,
            limit,
            order,
            -(-lower // 1000000) if lower is not None else None,
            -(-upper // 1000000) if upper is not None else None,
            include_content,
        )

    def matches(self, item: dict[str, Any]) -> bool:
        if item["id"] <= self.after_id or (
            self.before_id is not None and item["id"] >= self.before_id
        ):
            return False
        sent_at = item["sent_at"]
        if (self.since is not None or self.until is not None) and sent_at is None:
            return False
        return (self.since is None or sent_at >= self.since) and (
            self.until is None or sent_at < self.until
        )
