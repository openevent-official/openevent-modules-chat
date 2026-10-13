"""Value objects shared by the stateless parser and Chat client."""
from dataclasses import dataclass, field
from typing import Any

from ._json_spans import validate_object


@dataclass(frozen=True)
class RawExtensions:
    """Validated, immutable JSON object text whose values remain opaque to Chat."""
    json_text: str = field(repr=False)

    def __post_init__(self):
        validate_object(self.json_text)

    @classmethod
    def _from_validated(cls, json_text):
        # The payload scanner already checked this exact object span.
        value = object.__new__(cls)
        object.__setattr__(value, 'json_text', json_text)
        return value


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
