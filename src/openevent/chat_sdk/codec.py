from __future__ import annotations

import json
import math
from typing import Any, Iterable

from .errors import ChatProtocolError, InvalidKindError, MalformedPayloadError
from .model import (
    KIND_TURN_APPEND,
    KIND_TURN_CANCEL,
    KIND_TURN_END,
    KIND_TURN_START,
    ChatEvent,
    ObjectKey,
    ParsedMessage,
    TextPart,
    TurnAppend,
    TurnCancel,
    TurnEnd,
    TurnRef,
    TurnStart,
    require_turn_id,
    require_uint64,
)


class _StrictJsonError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _StrictJsonError(f"non-finite JSON number: {value}")


def _validate_json_tree(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ChatProtocolError("JSON strings must contain valid Unicode scalar values") from exc
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ChatProtocolError("JSON object keys must be strings")
                stack.extend((key, child))
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ChatProtocolError("JSON numbers must be finite")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ChatProtocolError("value is not JSON encodable")


def _extensions(data: dict[str, Any]) -> dict[str, Any] | None:
    if "extensions" not in data:
        return None
    value = data["extensions"]
    if not isinstance(value, dict):
        raise ChatProtocolError("extensions must be a JSON object")
    return value


def _exact_fields(data: dict[str, Any], required: set[str]) -> None:
    allowed = required | {"extensions"}
    if set(data) != required and set(data) != allowed:
        missing = sorted(required - set(data))
        extra = sorted(set(data) - allowed)
        detail = []
        if missing:
            detail.append(f"missing fields {missing}")
        if extra:
            detail.append(f"unknown fields {extra}")
        raise ChatProtocolError("; ".join(detail) or "invalid field set")


def _turn_ref(value: Any, name: str = "TurnRef") -> TurnRef:
    if not isinstance(value, dict) or set(value) != {"principal", "turn_id"}:
        raise ChatProtocolError(f"{name} must contain exactly principal and turn_id")
    return TurnRef(principal=require_uint64(value["principal"], f"{name}.principal"), turn_id=require_turn_id(value["turn_id"]))


def _content(value: Any) -> tuple[TextPart, ...]:
    if not isinstance(value, list) or not value:
        raise ChatProtocolError("content must be a non-empty JSON array")
    parts: list[TextPart] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"type", "text"}:
            raise ChatProtocolError("each content part must contain exactly type and text")
        if item["type"] != "text":
            raise ChatProtocolError("content part type must be text")
        parts.append(TextPart(text=item["text"]))
    return tuple(parts)


def parse_payload(payload: bytes) -> ChatEvent:
    if not isinstance(payload, bytes):
        raise MalformedPayloadError("payload must be bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
        data = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MalformedPayloadError("payload is not strict UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise MalformedPayloadError("payload must be a JSON object")
    _validate_json_tree(data)
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {KIND_TURN_START, KIND_TURN_APPEND, KIND_TURN_END, KIND_TURN_CANCEL}:
        raise InvalidKindError("payload kind is missing or unknown")

    if kind == KIND_TURN_START:
        _exact_fields(data, {"kind", "turn_id", "reply_to_turns", "content"})
        turn_id = require_turn_id(data["turn_id"])
        replies_value = data["reply_to_turns"]
        if not isinstance(replies_value, list):
            raise ChatProtocolError("reply_to_turns must be a JSON array")
        replies = tuple(_turn_ref(item, "reply_to_turns item") for item in replies_value)
        if len(set(replies)) != len(replies):
            raise ChatProtocolError("reply_to_turns must not contain duplicates")
        return TurnStart(turn_id=turn_id, reply_to_turns=replies, content=_content(data["content"]), extensions=_extensions(data))

    if kind == KIND_TURN_APPEND:
        _exact_fields(data, {"kind", "turn_id", "pre_seq", "content"})
        return TurnAppend(
            turn_id=require_turn_id(data["turn_id"]),
            pre_seq=require_uint64(data["pre_seq"], "pre_seq", nonzero=True),
            content=_content(data["content"]),
            extensions=_extensions(data),
        )

    if kind == KIND_TURN_END:
        _exact_fields(data, {"kind", "turn_id", "pre_seq", "status"})
        if data["status"] != "completed":
            raise ChatProtocolError("turn.end status must be completed")
        return TurnEnd(
            turn_id=require_turn_id(data["turn_id"]),
            pre_seq=require_uint64(data["pre_seq"], "pre_seq", nonzero=True),
            extensions=_extensions(data),
        )

    _exact_fields(data, {"kind", "target_turn"})
    return TurnCancel(target_turn=_turn_ref(data["target_turn"], "target_turn"), extensions=_extensions(data))


def _part_dict(part: TextPart) -> dict[str, str]:
    if not isinstance(part, TextPart):
        raise ChatProtocolError("content items must be TextPart values")
    return {"type": "text", "text": part.text}


def _ref_dict(turn_ref: TurnRef) -> dict[str, Any]:
    if not isinstance(turn_ref, TurnRef):
        raise ChatProtocolError("turn references must be TurnRef values")
    return {"principal": turn_ref.principal, "turn_id": turn_ref.turn_id}


def encode_payload(data: dict[str, Any]) -> bytes:
    _validate_json_tree(data)
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ChatProtocolError("payload cannot be encoded as strict UTF-8 JSON") from exc


def encode_start(turn_ref: TurnRef, replies: Iterable[TurnRef], content: Iterable[TextPart], extensions: dict[str, Any] | None) -> bytes:
    reply_tuple = tuple(replies)
    if len(set(reply_tuple)) != len(reply_tuple):
        raise ChatProtocolError("reply_to_turns must not contain duplicates")
    if turn_ref in reply_tuple:
        raise ChatProtocolError("reply_to_turns must not contain the turn being created")
    content_tuple = tuple(content)
    if not content_tuple:
        raise ChatProtocolError("content must not be empty")
    data: dict[str, Any] = {
        "kind": KIND_TURN_START,
        "turn_id": turn_ref.turn_id,
        "reply_to_turns": [_ref_dict(item) for item in reply_tuple],
        "content": [_part_dict(item) for item in content_tuple],
    }
    if extensions is not None:
        if not isinstance(extensions, dict):
            raise ChatProtocolError("extensions must be a JSON object")
        data["extensions"] = extensions
    return encode_payload(data)


def encode_append(turn_id: str, pre_seq: int, content: Iterable[TextPart], extensions: dict[str, Any] | None) -> bytes:
    content_tuple = tuple(content)
    if not content_tuple:
        raise ChatProtocolError("content must not be empty")
    data: dict[str, Any] = {
        "kind": KIND_TURN_APPEND,
        "turn_id": require_turn_id(turn_id),
        "pre_seq": require_uint64(pre_seq, "pre_seq", nonzero=True),
        "content": [_part_dict(item) for item in content_tuple],
    }
    if extensions is not None:
        if not isinstance(extensions, dict):
            raise ChatProtocolError("extensions must be a JSON object")
        data["extensions"] = extensions
    return encode_payload(data)


def encode_end(turn_id: str, pre_seq: int, extensions: dict[str, Any] | None) -> bytes:
    data: dict[str, Any] = {
        "kind": KIND_TURN_END,
        "turn_id": require_turn_id(turn_id),
        "pre_seq": require_uint64(pre_seq, "pre_seq", nonzero=True),
        "status": "completed",
    }
    if extensions is not None:
        if not isinstance(extensions, dict):
            raise ChatProtocolError("extensions must be a JSON object")
        data["extensions"] = extensions
    return encode_payload(data)


def encode_cancel(target_turn: TurnRef, extensions: dict[str, Any] | None) -> bytes:
    data: dict[str, Any] = {"kind": KIND_TURN_CANCEL, "target_turn": _ref_dict(target_turn)}
    if extensions is not None:
        if not isinstance(extensions, dict):
            raise ChatProtocolError("extensions must be a JSON object")
        data["extensions"] = extensions
    return encode_payload(data)


def parse_message(message: Any) -> ParsedMessage:
    seq = require_uint64(getattr(message, "seq", None), "EventMessage.seq", nonzero=True)
    ts_ms = require_uint64(getattr(message, "ts_ms", None), "EventMessage.ts_ms")
    channel_id = require_uint64(getattr(message, "channel_id", None), "EventMessage.channel_id", nonzero=True)
    principal = require_uint64(getattr(message, "principal", None), "EventMessage.principal")
    try:
        recipients = tuple(require_uint64(value, "EventMessage.recipient") for value in message.recipients)
        raw_keys = tuple(message.object_keys)
    except (AttributeError, TypeError) as exc:
        raise ChatProtocolError("EventMessage recipients and object_keys must be sequences") from exc
    if len(raw_keys) > 1024:
        raise ChatProtocolError("EventMessage contains more than 1024 ObjectKeys")
    try:
        object_keys = tuple(ObjectKey(object_id=key.object_id, object_token=key.object_token) for key in raw_keys)
    except AttributeError as exc:
        raise ChatProtocolError("EventMessage ObjectKeys are malformed") from exc
    event = parse_payload(getattr(message, "payload", None))
    turn_ref = event.target_turn if isinstance(event, TurnCancel) else TurnRef(principal=principal, turn_id=event.turn_id)
    return ParsedMessage(
        seq=seq,
        ts_ms=ts_ms,
        channel_id=channel_id,
        principal=principal,
        recipients=recipients,
        object_keys=object_keys,
        payload=event,
        turn_ref=turn_ref,
    )
