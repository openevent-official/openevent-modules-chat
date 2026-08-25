from __future__ import annotations

import json
import math
from typing import Any, Iterable

from .errors import ChatProtocolError, InvalidKindError, MalformedPayloadError
from .model import (
    KIND_TURN_APPEND,
    KIND_TURN_CANCEL,
    KIND_TURN_END,
    KIND_TURN_SINGLE,
    KIND_TURN_START,
    ChatEvent,
    ObjectKey,
    ParsedMessage,
    TextPart,
    TurnAppend,
    TurnCancel,
    TurnEnd,
    TurnRef,
    TurnSingle,
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
    return TurnRef(require_uint64(value["principal"], f"{name}.principal"), require_turn_id(value["turn_id"]))


def _replies(value: Any) -> tuple[TurnRef, ...]:
    if not isinstance(value, list):
        raise ChatProtocolError("reply_to_turns must be a JSON array")
    result = tuple(_turn_ref(item, "reply_to_turns item") for item in value)
    if len(set(result)) != len(result):
        raise ChatProtocolError("reply_to_turns must not contain duplicates")
    return result


def _content(value: Any, *, required_nonempty: bool) -> tuple[TextPart, ...]:
    if not isinstance(value, list):
        raise ChatProtocolError("content must be a JSON array")
    if required_nonempty and not value:
        raise ChatProtocolError("content must be a non-empty JSON array")
    parts: list[TextPart] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"type", "text"}:
            raise ChatProtocolError("each content part must contain exactly type and text")
        if item["type"] != "text":
            raise ChatProtocolError("content part type must be text")
        parts.append(TextPart(item["text"]))
    return tuple(parts)


def parse_payload(payload: bytes) -> ChatEvent:
    if not isinstance(payload, bytes):
        raise MalformedPayloadError("payload must be bytes")
    try:
        data = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MalformedPayloadError("payload is not strict UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise MalformedPayloadError("payload must be a JSON object")
    _validate_json_tree(data)
    kind = data.get("kind")
    kinds = {KIND_TURN_SINGLE, KIND_TURN_START, KIND_TURN_APPEND, KIND_TURN_END, KIND_TURN_CANCEL}
    if not isinstance(kind, str) or kind not in kinds:
        raise InvalidKindError("payload kind is missing or unknown")

    if kind == KIND_TURN_SINGLE:
        _exact_fields(data, {"kind", "turn_id", "reply_to_turns", "content"})
        return TurnSingle(require_turn_id(data["turn_id"]), _replies(data["reply_to_turns"]), _content(data["content"], required_nonempty=False), _extensions(data))
    if kind == KIND_TURN_START:
        _exact_fields(data, {"kind", "turn_id", "reply_to_turns", "content"})
        return TurnStart(require_turn_id(data["turn_id"]), _replies(data["reply_to_turns"]), _content(data["content"], required_nonempty=True), _extensions(data))
    if kind == KIND_TURN_APPEND:
        _exact_fields(data, {"kind", "turn_id", "pre_seq", "content"})
        return TurnAppend(require_turn_id(data["turn_id"]), require_uint64(data["pre_seq"], "pre_seq", nonzero=True), _content(data["content"], required_nonempty=True), _extensions(data))
    if kind == KIND_TURN_END:
        _exact_fields(data, {"kind", "turn_id", "pre_seq", "status"})
        if data["status"] != "completed":
            raise ChatProtocolError("turn.end status must be completed")
        return TurnEnd(require_turn_id(data["turn_id"]), require_uint64(data["pre_seq"], "pre_seq", nonzero=True), _extensions(data))
    _exact_fields(data, {"kind", "target_turn"})
    return TurnCancel(_turn_ref(data["target_turn"], "target_turn"), _extensions(data))


def encode_payload(data: dict[str, Any]) -> bytes:
    _validate_json_tree(data)
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ChatProtocolError("payload cannot be encoded as strict UTF-8 JSON") from exc


def _part_dict(part: TextPart) -> dict[str, str]:
    if not isinstance(part, TextPart):
        raise ChatProtocolError("content items must be TextPart values")
    return {"type": "text", "text": part.text}


def _ref_dict(ref: TurnRef) -> dict[str, Any]:
    if not isinstance(ref, TurnRef):
        raise ChatProtocolError("turn references must be TurnRef values")
    return {"principal": ref.principal, "turn_id": ref.turn_id}


def _with_extensions(data: dict[str, Any], extensions: dict[str, Any] | None) -> dict[str, Any]:
    if extensions is not None:
        if not isinstance(extensions, dict):
            raise ChatProtocolError("extensions must be a JSON object")
        data["extensions"] = extensions
    return data


def encode_single(turn_ref: TurnRef, replies: Iterable[TurnRef], content: Iterable[TextPart], extensions: dict[str, Any] | None) -> bytes:
    replies = tuple(replies)
    if len(set(replies)) != len(replies) or turn_ref in replies:
        raise ChatProtocolError("reply_to_turns must be unique and cannot contain the created turn")
    parts = tuple(content)
    return encode_payload(_with_extensions({"kind": KIND_TURN_SINGLE, "turn_id": require_turn_id(turn_ref.turn_id), "reply_to_turns": [_ref_dict(r) for r in replies], "content": [_part_dict(p) for p in parts]}, extensions))


def encode_start(turn_ref: TurnRef, replies: Iterable[TurnRef], content: Iterable[TextPart], extensions: dict[str, Any] | None) -> bytes:
    replies = tuple(replies)
    if len(set(replies)) != len(replies) or turn_ref in replies:
        raise ChatProtocolError("reply_to_turns must be unique and cannot contain the created turn")
    parts = tuple(content)
    if not parts:
        raise ChatProtocolError("content must not be empty")
    return encode_payload(_with_extensions({"kind": KIND_TURN_START, "turn_id": turn_ref.turn_id, "reply_to_turns": [_ref_dict(r) for r in replies], "content": [_part_dict(p) for p in parts]}, extensions))


def encode_append(turn_id: str, pre_seq: int, content: Iterable[TextPart], extensions: dict[str, Any] | None) -> bytes:
    parts = tuple(content)
    if not parts:
        raise ChatProtocolError("content must not be empty")
    return encode_payload(_with_extensions({"kind": KIND_TURN_APPEND, "turn_id": require_turn_id(turn_id), "pre_seq": require_uint64(pre_seq, "pre_seq", nonzero=True), "content": [_part_dict(p) for p in parts]}, extensions))


def encode_end(turn_id: str, pre_seq: int, extensions: dict[str, Any] | None) -> bytes:
    return encode_payload(_with_extensions({"kind": KIND_TURN_END, "turn_id": require_turn_id(turn_id), "pre_seq": require_uint64(pre_seq, "pre_seq", nonzero=True), "status": "completed"}, extensions))


def encode_cancel(target_turn: TurnRef, extensions: dict[str, Any] | None) -> bytes:
    return encode_payload(_with_extensions({"kind": KIND_TURN_CANCEL, "target_turn": _ref_dict(target_turn)}, extensions))


def parse_message(message: Any) -> ParsedMessage:
    try:
        seq = require_uint64(message.seq, "EventMessage.seq", nonzero=True)
        ts_ms = require_uint64(message.ts_ms, "EventMessage.ts_ms")
        channel_id = require_uint64(message.channel_id, "EventMessage.channel_id", nonzero=True)
        principal = require_uint64(message.principal, "EventMessage.principal")
        recipients = tuple(require_uint64(v, "EventMessage.recipient") for v in message.recipients)
        raw_keys = tuple(message.object_keys)
    except (AttributeError, TypeError) as exc:
        raise ChatProtocolError("EventMessage envelope is malformed") from exc
    if len(raw_keys) > 1024:
        raise ChatProtocolError("EventMessage contains more than 1024 ObjectKeys")
    try:
        keys = tuple(ObjectKey(k.object_id, k.object_token) for k in raw_keys)
        event = parse_payload(message.payload)
    except AttributeError as exc:
        raise ChatProtocolError("EventMessage ObjectKeys are malformed") from exc
    ref = event.target_turn if isinstance(event, TurnCancel) else TurnRef(principal, event.turn_id)
    return ParsedMessage(seq, ts_ms, channel_id, principal, recipients, keys, event, ref)
