# Chat Protocol chat.v1

[中文版](CHAT_PROTOCOL_cn.md)

> Status: initial specification
> Scope: event payloads for OpenEvent channels with `protocol="chat.v1"`

## 1. Protocol Boundary

`chat.v1` is a low-level event protocol for persistent conversations between
users and agents. One OpenEvent channel represents one permanent conversation
and may contain multiple user principals and multiple agent principals.

The channel history does not identify which principals are users or agents.
Role mapping is an application concern outside this protocol. The protocol
records only participant-visible interaction events; agent internals, hidden
reasoning, model requests, tool internals, secrets, and debug logs are outside
its scope.

The protocol defines exactly three event kinds:

- `turn.append`
- `turn.end`
- `turn.cancel`

Additional event kinds are not part of `chat.v1`. Applications may attach
visible metadata through the optional `extensions` object, but extensions do
not change the base turn semantics.

OpenEvent does not parse the JSON payload. This specification defines whether
an event is well-formed, but does not require applications to reject, skip,
display, or stop on malformed or conflicting history.

## 2. Channel And OpenEvent Fields

Every Chat channel MUST set:

```text
ChannelInfo.protocol = "chat.v1"
```

The payload has no version field. `ChannelInfo.description` has no `chat.v1`
schema and MAY contain application-defined text.

The protocol imposes no additional rule on channel visibility or membership.
Members may change over time. OpenEvent's current ACL determines who can read
or publish each event.

OpenEvent top-level fields keep their native meaning:

- `EventMessage.seq` is the authoritative global event order.
- `EventMessage.principal` is the publisher. For turn content it also
  establishes the content owner.
- `EventMessage.ts_ms` is the server receive time.
- `EventMessage.recipients` is freely usable for application-defined semantics.
  Its values still have to satisfy the OpenEvent server's publish validation;
  `chat.v1` does not require the list to be empty, stable within a turn, or
  related to replies.
- `EventMessage.object_keys` MAY be present. Their meaning is application-
  defined and the base protocol does not associate them with a content part.

OpenEvent recipients are a filtering field, not an ACL. A reader that needs to
reconstruct the complete conversation SHOULD read the channel with
`only_my_recipient=false`.

## 3. JSON And Common Fields

Every `chat.v1` payload MUST be a UTF-8 JSON object.

Common fields:

| Field | Rule |
| --- | --- |
| `kind` | Required string; exactly `turn.append`, `turn.end`, or `turn.cancel` |
| `turn_id` | Required non-empty string, at most 128 UTF-8 bytes |
| `extensions` | Optional JSON object interpreted only by the application |

`turn_id` equality is exact string equality. Producers and consumers MUST NOT
trim, case-fold, or Unicode-normalize it. A `turn_id` identifies at most one
turn within a channel; its uniqueness scope is `(channel_id, turn_id)`. The
protocol does not prescribe how applications generate it.

Fields referencing an OpenEvent sequence use JSON integers in
`1..18446744073709551615` (`uint64` excluding zero).

Unknown top-level payload fields are not part of `chat.v1`. Application data
MUST be placed under `extensions`. Extension values may be any JSON value, but
must remain participant-visible interaction metadata and must not redefine
`kind`, `turn_id`, `pre_seq`, reply, content, or terminal semantics.

The OpenEvent deployment controls the payload size limit. `chat.v1` does not
define a second byte limit.

## 4. Content Parts

The first version supports text parts only:

```json
{
  "type": "text",
  "text": "Hello"
}
```

A content value MUST be a non-empty JSON array. Each item MUST be an object
containing exactly:

- `type`: the string `text`;
- `text`: a non-empty string.

Part order is significant. An application reconstructs visible turn text by
walking valid append events in chain order and, within each event, preserving
the listed part order. Chunk boundaries have no semantic meaning: applications
may append characters, words, sentences, or larger text fragments.

Binary, image, audio, tool-call, and other content part types are not defined.
An application may attach ObjectKeys to the containing OpenEvent message, but
that does not introduce another `chat.v1` content type.

## 5. `turn.append`

`turn.append` creates a turn or appends content to an existing turn.

### 5.1 First Append

```json
{
  "kind": "turn.append",
  "turn_id": "turn_01JABC",
  "reply_to_turn_ids": ["turn_01JAAA", "turn_01JAAB"],
  "content": [
    {"type": "text", "text": "Hello"}
  ]
}
```

Rules:

- `pre_seq` MUST be absent.
- `reply_to_turn_ids` MUST be present as a JSON array and MAY be empty.
- Every reply ID MUST satisfy the `turn_id` string rules.
- Reply IDs MUST be unique within the array and MUST NOT equal this event's
  `turn_id`.
- Every referenced turn MUST already have a first append in the same channel.
  It may still be open or may already be terminal.
- `content` MUST satisfy Section 4.
- The OpenEvent top-level `principal` becomes the content owner of this turn.

The first append fixes the turn's owner and `reply_to_turn_ids` for its entire
lifetime.

### 5.2 Later Append

```json
{
  "kind": "turn.append",
  "turn_id": "turn_01JABC",
  "pre_seq": 123456,
  "content": [
    {"type": "text", "text": " world"}
  ]
}
```

Rules:

- `pre_seq` MUST be present and equal the OpenEvent `seq` of the immediately
  preceding valid `turn.append` for this turn.
- `reply_to_turn_ids` MUST be absent.
- The OpenEvent top-level `principal` MUST equal the owner established by the
  first append.
- `content` MUST satisfy Section 4.
- The event MUST precede the turn's earliest terminal event.

## 6. `turn.end`

`turn.end` normally completes a turn.

```json
{
  "kind": "turn.end",
  "turn_id": "turn_01JABC",
  "pre_seq": 123457,
  "status": "completed"
}
```

Rules:

- The turn MUST already have a first append.
- `pre_seq` MUST be present and equal the OpenEvent `seq` of the immediately
  preceding valid `turn.append` for this turn.
- The OpenEvent top-level `principal` MUST equal the turn owner.
- `status` MUST be exactly `completed`. `chat.v1` has no `failed` status.
- `content` and `reply_to_turn_ids` MUST be absent.
- The event stores only completion state; it does not repeat a final content
  snapshot.

## 7. `turn.cancel`

`turn.cancel` independently cancels an existing turn.

```json
{
  "kind": "turn.cancel",
  "turn_id": "turn_01JABC"
}
```

Rules:

- The target turn MUST already have a first append in the same channel.
- `pre_seq`, `status`, `content`, and `reply_to_turn_ids` MUST be absent.
- Any principal allowed by OpenEvent to publish to the channel MAY publish the
  event. It need not be the turn owner or have a user/agent role known to the
  protocol.
- Cancellation itself is a terminal event; no later `turn.end` acknowledgement
  is required.

## 8. Chain And Terminal Semantics

For one turn, valid `turn.append` events and a valid `turn.end` form a single
chain through `pre_seq`.

- A first append has no predecessor.
- A later append or normal end names the current append tail.
- At most one append or end may name a given `pre_seq` as its predecessor.
  Multiple successors are a fork and violate the protocol.
- `turn.cancel` is not a chain successor and never has `pre_seq`.

A turn's terminal event is the event with the smallest OpenEvent global `seq`
among its well-formed `turn.end` and `turn.cancel` events. Therefore concurrent
completion and cancellation have one deterministic outcome shared by all
readers.

Only valid append events preceding that terminal `seq` contribute to the
resolved content. Later append, end, or cancel records remain in immutable
OpenEvent history but do not change the resolved turn. This protocol does not
mandate how an application reports or handles those records.

Different turns may be open and append concurrently. Their events may interleave
in global OpenEvent order; each turn's `pre_seq` chain determines its local
content order.

## 9. Replies

`reply_to_turn_ids` records causal context, not routing or authorization.

- It is fixed by the first append and cannot be modified later.
- An empty array represents a turn with no protocol-level parent.
- Multiple IDs allow one turn to respond to several earlier turns.
- A referenced turn does not need to be terminal, allowing input and output to
  overlap.
- Replies do not imply any value for OpenEvent `recipients`.

Because every reply target must already exist, reply edges always point to an
earlier first append in the same channel and cannot form cycles.

## 10. Application Responsibilities

The application, not `chat.v1`, is responsible for:

- mapping principals to user or agent roles;
- channel visibility, membership changes, and authorization policy;
- recipients and event routing;
- interpreting `extensions` and ObjectKeys;
- publish reconciliation, retries, deduplication, and idempotency;
- choosing behavior for malformed JSON, unknown kinds, missing references,
  broken chains, forks, and events after termination;
- rendering concurrent turns and cancellation state.

There is no protocol `event_id`. Applications that retry after an uncertain
publish result must reconcile against OpenEvent history or accept the risk of a
duplicate record or an invalid fork.

## 11. Security And Persistence

- Channel ACL, not recipients, is the confidentiality boundary.
- A newly added channel member may be able to read the entire retained history.
- Removing a member cannot revoke data or ObjectKeys already obtained.
- ObjectKeys are bearer capabilities and may be permanently transferable.
- Any principal with channel write permission can cancel any existing turn.
- Chat events are append-only. The protocol defines no edit, delete, redaction,
  or retention operation.

Applications must not place hidden reasoning, credentials, private Agent state,
or other data that participants must not read into payloads, extensions, or
attached objects.
