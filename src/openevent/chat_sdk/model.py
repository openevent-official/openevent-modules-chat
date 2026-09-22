"""Value objects shared by the stateless parser and Chat client."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class TurnRef:
    principal: int
    turn_id: str


@dataclass(frozen=True)
class ObjectKey:
    object_id: int
    object_token: str = field(repr=False)


@dataclass(frozen=True)
class ParsedMessage:
    seq: int
    channel_id: int
    principal: int
    ts_ms: int
    uuid: int
    recipients: tuple[int, ...]
    object_keys: tuple[ObjectKey, ...]
    payload: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class FetchPage:
    messages: tuple[ParsedMessage, ...]
    next_seq: int
    last_seq: int
