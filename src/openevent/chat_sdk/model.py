from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ChatProtocolError


UINT64_MAX = (1 << 64) - 1
KIND_TURN_START = "turn.start"
KIND_TURN_APPEND = "turn.append"
KIND_TURN_END = "turn.end"
KIND_TURN_CANCEL = "turn.cancel"


def require_uint64(value: Any, name: str, *, nonzero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < int(nonzero) or value > UINT64_MAX:
        lower = 1 if nonzero else 0
        raise ChatProtocolError(f"{name} must be an integer in {lower}..{UINT64_MAX}")
    return value


def require_turn_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ChatProtocolError("turn_id must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ChatProtocolError("turn_id must contain valid Unicode scalar values") from exc
    if len(encoded) > 128:
        raise ChatProtocolError("turn_id must be at most 128 UTF-8 bytes")
    return value


@dataclass(frozen=True)
class TextPart:
    text: str
    type: str = field(default="text", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ChatProtocolError("text part text must be a non-empty string")
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ChatProtocolError("text part must contain valid Unicode scalar values") from exc


@dataclass(frozen=True)
class TurnRef:
    principal: int
    turn_id: str

    def __post_init__(self) -> None:
        require_uint64(self.principal, "principal")
        require_turn_id(self.turn_id)


@dataclass(frozen=True)
class ObjectKey:
    object_id: int
    object_token: str = field(repr=False)

    def __post_init__(self) -> None:
        require_uint64(self.object_id, "object_id", nonzero=True)
        if not isinstance(self.object_token, str) or not self.object_token:
            raise ChatProtocolError("object_token must be a non-empty string")

    def __repr__(self) -> str:
        return f"ObjectKey(object_id={self.object_id}, object_token=<redacted>)"


@dataclass(frozen=True)
class TurnStart:
    turn_id: str
    reply_to_turns: tuple[TurnRef, ...]
    content: tuple[TextPart, ...]
    extensions: dict[str, Any] | None = None
    kind: str = field(default=KIND_TURN_START, init=False)


@dataclass(frozen=True)
class TurnAppend:
    turn_id: str
    pre_seq: int
    content: tuple[TextPart, ...]
    extensions: dict[str, Any] | None = None
    kind: str = field(default=KIND_TURN_APPEND, init=False)


@dataclass(frozen=True)
class TurnEnd:
    turn_id: str
    pre_seq: int
    extensions: dict[str, Any] | None = None
    status: str = field(default="completed", init=False)
    kind: str = field(default=KIND_TURN_END, init=False)


@dataclass(frozen=True)
class TurnCancel:
    target_turn: TurnRef
    extensions: dict[str, Any] | None = None
    kind: str = field(default=KIND_TURN_CANCEL, init=False)


ChatEvent = TurnStart | TurnAppend | TurnEnd | TurnCancel


@dataclass(frozen=True)
class ParsedMessage:
    seq: int
    ts_ms: int
    channel_id: int
    principal: int
    recipients: tuple[int, ...]
    object_keys: tuple[ObjectKey, ...]
    payload: ChatEvent
    turn_ref: TurnRef

    @property
    def event(self) -> ChatEvent:
        return self.payload

