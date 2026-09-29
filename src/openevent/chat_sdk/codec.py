"""Strict, single-event chat.v1 encoding and parsing; no history state."""
import json
import math

from .errors import ChatProtocolError
from .model import ObjectKey, ParsedMessage, TextPart

UINT64_MAX = (1 << 64) - 1


def validate_uint64(value, field, positive=True):
    if type(value) is not int or not (int(positive) <= value <= UINT64_MAX):
        raise ChatProtocolError(f"{field} must be {'positive ' if positive else ''}uint64")
    return value


def _string(value, field, *, nonempty=False, max_bytes=None):
    if not isinstance(value, str) or (nonempty and not value):
        raise ChatProtocolError(f"{field} must be {'nonempty ' if nonempty else ''}string")
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise ChatProtocolError(f"{field} must be valid UTF-8") from None
    if max_bytes is not None and size > max_bytes:
        raise ChatProtocolError(f"{field} exceeds UTF-8 byte limit")
    return value


def validate_turn_id(value):
    return _string(value, 'turn_id', nonempty=True, max_bytes=128)


def _fields(value, required, field):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ChatProtocolError(f"{field} has missing or unknown fields")


def _json_value(value):
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, str):
        _string(value, 'extensions string')
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _string(key, 'extensions key')
            _json_value(item)
        return
    raise ChatProtocolError('extensions must contain JSON values')


def validate_payload(payload, *, object_keys=()):
    if not isinstance(payload, dict):
        raise ChatProtocolError('payload must be JSON object')
    kind = payload.get('kind')
    fields = {
        'turn.single': {'kind', 'turn_id', 'reply_to_seqs', 'content'},
        'turn.start': {'kind', 'turn_id', 'reply_to_seqs', 'content'},
        'turn.append': {'kind', 'turn_id', 'pre_seq', 'content'},
        'turn.reset': {'kind', 'turn_id', 'pre_seq', 'content'},
        'turn.end': {'kind', 'turn_id', 'pre_seq'},
        'turn.cancel': {'kind', 'target_turn'},
        'submission.reserve': {'kind', 'reserved_through'},
    }
    if not isinstance(kind, str) or kind not in fields:
        raise ChatProtocolError('kind is not supported')
    required = fields[kind]
    if not required <= payload.keys() or payload.keys() - required - {'extensions'}:
        raise ChatProtocolError('payload has missing or unknown fields')
    if 'turn_id' in payload:
        validate_turn_id(payload['turn_id'])
    if 'pre_seq' in payload:
        validate_uint64(payload['pre_seq'], 'pre_seq')
    if 'reserved_through' in payload:
        validate_uint64(payload['reserved_through'], 'reserved_through')
    if 'target_turn' in payload:
        target = payload['target_turn']
        _fields(target, {'principal', 'turn_id'}, 'target_turn')
        validate_uint64(target['principal'], 'target_turn.principal')
        validate_turn_id(target['turn_id'])
    if 'reply_to_seqs' in payload:
        replies = payload['reply_to_seqs']
        if not isinstance(replies, list):
            raise ChatProtocolError('reply_to_seqs must be array')
        for seq in replies:
            validate_uint64(seq, 'reply_to_seqs item')
        if len(set(replies)) != len(replies):
            raise ChatProtocolError('reply_to_seqs contains duplicates')
    if 'content' in payload:
        content = payload['content']
        if not isinstance(content, list):
            raise ChatProtocolError('content must be array')
        if kind == 'turn.append' and not content and not object_keys:
            raise ChatProtocolError('turn.append requires content or object_keys')
        for part in content:
            _fields(part, {'type', 'text'}, 'content part')
            if part['type'] != 'text':
                raise ChatProtocolError('content part type must be text')
            _string(part['text'], 'content text', nonempty=True)
    if kind == 'submission.reserve' and object_keys:
        raise ChatProtocolError('submission.reserve cannot contain object_keys')
    if 'extensions' in payload:
        if not isinstance(payload['extensions'], dict):
            raise ChatProtocolError('extensions must be object')
        try:
            _json_value(payload['extensions'])
        except RecursionError:
            raise ChatProtocolError('extensions contains a cycle or excessive nesting') from None
    return payload


def make_content(parts):
    try:
        result = []
        for part in parts:
            if not isinstance(part, TextPart):
                raise ChatProtocolError('content entries must be TextPart')
            result.append({'type': 'text', 'text': part.text})
        return result
    except TypeError:
        raise ChatProtocolError('content must be iterable') from None


def encode_payload(payload, *, object_keys=()):
    validate_payload(payload, object_keys=object_keys)
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ChatProtocolError('payload cannot be encoded as UTF-8 JSON') from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ChatProtocolError('JSON object contains duplicate fields')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ChatProtocolError('JSON contains non-finite number')


def parse_message(event_message):
    try:
        seq = validate_uint64(event_message.seq, 'seq')
        channel = validate_uint64(event_message.channel_id, 'channel_id')
        principal = validate_uint64(event_message.principal, 'principal')
        timestamp = validate_uint64(event_message.ts_ms, 'ts_ms', positive=False)
        uuid = validate_uint64(event_message.uuid, 'uuid')
        recipients = tuple(validate_uint64(p, 'recipient')
                           for p in event_message.recipients)
        objects = tuple(ObjectKey(validate_uint64(k.object_id, 'object_id'),
                                  _string(k.object_token, 'object_token', nonempty=True))
                        for k in event_message.object_keys)
        if len(objects) > 1024:
            raise ChatProtocolError('object_keys exceeds 1024 entries')
        if not isinstance(event_message.payload, bytes):
            raise ChatProtocolError('payload must be UTF-8 bytes')
        payload = json.loads(event_message.payload.decode('utf-8'),
                             object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        validate_payload(payload, object_keys=objects)
        return ParsedMessage(seq, channel, principal, timestamp, uuid, recipients, objects, payload)
    except ChatProtocolError:
        raise
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise ChatProtocolError('event must contain valid chat.v1 JSON and envelope fields') from None
