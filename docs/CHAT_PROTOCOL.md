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

The protocol defines exactly five event kinds:

- `turn.single`
- `turn.start`
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

- `EventMessage.seq` is the authoritative global event order and the event
  position referenced by `pre_seq`; it is not a turn ID.
- `EventMessage.principal` is the publisher. A `turn.single` or `turn.start`
  publisher is the turn owner, and append/end publishers MUST equal that owner.
- `EventMessage.ts_ms` is the server receive time.
- `EventMessage.uuid` is the nonzero UUID allocated by OpenEvent for this one
  committed message. It is not a `turn_id`, TurnRef, `pre_seq`, or payload
  field. Allocation, consumption, duplicate rejection, and uncertain-result
  handling are defined only by the OpenEvent API contract.
- `EventMessage.recipients` is freely usable for application-defined semantics.
  Its values still have to satisfy the OpenEvent server's publish validation;
  `chat.v1` does not require the list to be empty, stable within a turn, or
  related to replies.
- `EventMessage.object_keys` MAY be present. Their meaning is application-
  defined and the base protocol does not associate them with a content part.

OpenEvent recipients are a filtering field, not an ACL. A reader that needs to
reconstruct the complete conversation SHOULD read the channel with
`only_my_recipient=false`.

## 3. JSON, TurnRef, And Common Fields

Every `chat.v1` payload MUST be a UTF-8 JSON object.

Common fields:

| Field | Rule |
| --- | --- |
| `kind` | Required string; exactly `turn.single`, `turn.start`, `turn.append`, `turn.end`, or `turn.cancel` |
| `turn_id` | Required for `turn.single`, `turn.start`, `turn.append`, and `turn.end`; MUST be absent from `turn.cancel` |
| `extensions` | Optional JSON object interpreted only by the application |

`turn_id` MUST be a non-empty string of at most 128 UTF-8 bytes. Producers and
consumers use exact string equality and MUST NOT trim, case-fold, or Unicode-
normalize it.

A turn is uniquely identified within its channel by this TurnRef:

```json
{
  "principal": 9001,
  "turn_id": "turn-01JABC"
}
```

A TurnRef MUST be a JSON object containing exactly:

- `principal`: the top-level OpenEvent principal of the `turn.single` or
  `turn.start` that created the turn, using the OpenEvent principal's `uint64`
  JSON integer rules;
- `turn_id`: a string satisfying the rules above.

TurnRef equality is exact `(principal, turn_id)` equality. The containing
channel is already known from the event location, so protocol text uses TurnRef
while a complete storage key is `(channel_id, principal, turn_id)`. A writer
MUST NOT create two turns with the same `turn_id` under its own principal in the
same channel. Different principals MAY use the same `turn_id`; those are
different turns.

The only payload field referencing an OpenEvent sequence is `pre_seq`. It uses
a non-zero `uint64` JSON integer in `1..18446744073709551615`.

Unknown top-level payload fields are not part of `chat.v1`. Application data
MUST be placed under `extensions`. Extension values may be any JSON value, but
must remain participant-visible interaction metadata and must not redefine
`kind`, `turn_id`, `pre_seq`, TurnRef, reply, content, or terminal semantics.

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

A content value MUST be a JSON array. `turn.single` MAY use an empty array to
support application-defined object-only attachment turns; `turn.start` and
`turn.append` MUST use a non-empty array. Each item MUST be an object
containing exactly:

- `type`: the string `text`;
- `text`: a non-empty string.

Part order is significant. For `turn.single`, an application uses the part
order in that event directly. For a streaming turn, it reconstructs visible
text by walking valid turn-chain events in chain order and, within each event,
preserving the listed part order. Chunk boundaries have no semantic meaning:
applications may append characters, words, sentences, or larger text
fragments.

Binary, image, audio, tool-call, and other content part types are not defined.
An application may attach ObjectKeys to the containing OpenEvent message, but
that does not introduce another `chat.v1` content type.

## 5. `turn.single`

`turn.single` creates and normally completes a non-appendable turn in one
event. It is intended for a complete user input submitted at once. The writer
generates `turn_id` before publication. The top-level OpenEvent principal and
payload `turn_id` together form the turn's TurnRef.

```json
{
  "kind": "turn.single",
  "turn_id": "turn-user-1",
  "reply_to_turns": [
    {"principal": 9002, "turn_id": "turn-agent-1"}
  ],
  "content": [
    {"type": "text", "text": "Hello"}
  ]
}
```

Rules:

- `turn_id` MUST be present and satisfy Section 3.
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, `reply_to_turns`, and `content`. Therefore `pre_seq`,
  `target_turn`, and `status` MUST be absent.
- `reply_to_turns` MUST be present as a JSON array and MAY be empty.
- Every item MUST be a valid TurnRef. TurnRefs MUST be unique within the array
  and MUST NOT equal the current turn's TurnRef.
- Every referenced turn MUST already have been created by an earlier
  `turn.single` or `turn.start` in the same channel. It MAY still be open or
  may already be terminal.
- `content` MUST satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- One channel permits only one creation event for a given owner principal and
  `turn_id`. If an earlier `turn.single` or `turn.start` exists, another event
  of either creation kind is a protocol conflict.

`turn.single` permanently fixes the TurnRef, owner, `reply_to_turns`, complete
content, and completed terminal state. It has no `pre_seq` or `status`. Its
OpenEvent `seq` is both the turn's `start_seq` and `terminal_seq`. No later
`turn.end` is needed or can complete it again.

## 6. `turn.start`

`turn.start` creates a streaming turn that permits later append events and is
the turn's first event. The writer generates `turn_id` before publication. The
top-level OpenEvent principal and payload `turn_id` together form the turn's
TurnRef.

```json
{
  "kind": "turn.start",
  "turn_id": "turn-user-1",
  "reply_to_turns": [
    {"principal": 9002, "turn_id": "turn-agent-1"}
  ],
  "content": [
    {"type": "text", "text": "Hello"}
  ]
}
```

Rules:

- `turn_id` MUST be present and satisfy Section 3.
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, `reply_to_turns`, and `content`. Therefore `pre_seq`,
  `target_turn`, and `status` MUST be absent.
- `reply_to_turns` MUST be present as a JSON array and MAY be empty.
- Every item MUST be a valid TurnRef. TurnRefs MUST be unique within the array
  and MUST NOT equal the current turn's TurnRef.
- Every referenced turn MUST already have been created by an earlier
  `turn.single` or `turn.start` in the same channel. It MAY still be open or
  may already be terminal.
- `content` MUST satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- One channel permits only one creation event for a given owner principal and
  `turn_id`. If an earlier `turn.single` or `turn.start` exists, another event
  of either creation kind is a protocol conflict.

`turn.start` fixes the turn's TurnRef, owner, and `reply_to_turns` for its entire
lifetime. It has no `pre_seq`; its OpenEvent `seq` is the starting point of the
subsequent append/end chain.

## 7. `turn.append`

`turn.append` appends content to an existing turn owned by the publishing
principal.

```json
{
  "kind": "turn.append",
  "turn_id": "turn-user-1",
  "pre_seq": 123402,
  "content": [
    {"type": "text", "text": " world"}
  ]
}
```

Rules:

- To have a turn-state effect, the target TurnRef MUST have been created by an
  earlier `turn.start` in the same channel and MUST still be non-terminal. A
  turn created by `turn.single` is already terminal, so a later append has no
  turn-state effect.
- For a non-terminal streaming turn, `pre_seq` MUST be present and equal the
  OpenEvent `seq` of the immediately preceding valid `turn.start` or
  `turn.append`.
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, `pre_seq`, and `content`. Therefore `reply_to_turns`,
  `target_turn`, and `status` MUST be absent.
- For a streaming turn on which the event has a state effect, the top-level
  principal MUST equal the owner established by `turn.start`.
- `content` MUST satisfy Section 4.

## 8. `turn.end`

`turn.end` normally completes a turn owned by the publishing principal.

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "pre_seq": 123403,
  "status": "completed"
}
```

Rules:

- To have a turn-state effect, the target TurnRef MUST have been created by an
  earlier `turn.start` in the same channel and MUST still be non-terminal. A
  turn created by `turn.single` is already completed, so a later `turn.end`
  has no turn-state effect.
- For a non-terminal streaming turn, `pre_seq` MUST be present and equal the
  OpenEvent `seq` of the immediately preceding valid `turn.start` or
  `turn.append`.
- `status` MUST be exactly `completed`.
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, `pre_seq`, and `status`. Therefore `content`, `reply_to_turns`, and
  `target_turn` MUST be absent.
- The event stores only completion state without repeating a final content
  snapshot.

## 9. `turn.cancel`

`turn.cancel` independently cancels an existing turn. Because the cancelling
principal need not be the turn owner, the payload uses a complete
`target_turn` TurnRef.

```json
{
  "kind": "turn.cancel",
  "target_turn": {
    "principal": 9002,
    "turn_id": "turn-agent-1"
  }
}
```

Rules:

- `target_turn` MUST be a valid TurnRef whose turn was already created by an
  earlier `turn.single` or `turn.start` in the same channel.
- Other than optional `extensions`, the payload MUST contain exactly `kind` and
  `target_turn`. Therefore top-level `turn_id`, `pre_seq`, `status`, `content`,
  and `reply_to_turns` MUST be absent.
- Any principal allowed by OpenEvent to publish to the channel MAY publish the
  event.
- Cancellation itself is terminal, so no later `turn.end` acknowledgement is
  required.

## 10. Chain And Terminal Semantics

A turn has one of two content and terminal forms:

- A turn created by `turn.single` has no append chain. That event contains all
  content and normally completes the turn at the same seq.
- A streaming turn created by `turn.start` uses the `pre_seq` chain below and
  is terminated by a later `turn.end` or `turn.cancel`.

For one TurnRef created by `turn.start`, the `turn.start`, valid `turn.append` events before the earliest
terminal event, and a valid `turn.end` when it is that earliest terminal event
form a single chain through `pre_seq`:

- `turn.start` is the head and has no predecessor. Its OpenEvent `seq` is the
  first chain tail, but is not the turn ID.
- A later append or normal end names the current append-chain tail.
- Before the earliest terminal event, at most one append or end may name a given
  `pre_seq` as its predecessor. Multiple successors are a fork and violate the
  protocol.
- `turn.cancel` associates through `target_turn`; it is not a chain successor
  and never has `pre_seq`.

For a `turn.single` turn, that event is its terminal event. For a turn created
by `turn.start`, the terminal event is the event with the smallest OpenEvent
global `seq` among its well-formed `turn.end` and `turn.cancel` events targeting
that TurnRef. Therefore concurrent completion and cancellation have one
deterministic outcome shared by all readers.

Events are processed in ascending OpenEvent `seq` order. Once a TurnRef is
terminal, later append, end, or cancel events targeting it cannot change the
resolved terminal state, final content, or pre-terminal chain tail. Even when
committed, those events have no turn-state effect. The protocol does not require
special parsing, field extraction, or validation precedence for events after a
terminal state, and applications do not need to classify whether they are
"valid" relative to that terminal state. Only valid start/append events before
the earliest terminal `seq` contribute to a streaming turn's resolved content;
a single turn's resolved content is exactly `turn.single.content`.

Different TurnRefs may be open and append concurrently. Their events may
interleave in global OpenEvent order; each turn's `pre_seq` chain determines its
local content order.

## 11. Replies

`reply_to_turns` is an array of TurnRefs recording causal context, not routing
or authorization.

- It is fixed by the creating `turn.single` or `turn.start` and cannot be
  modified later.
- An empty array represents a turn with no protocol-level parent.
- Multiple TurnRefs allow one turn to respond to several earlier turns.
- A referenced turn does not need to be terminal, allowing input and output to
  overlap.
- Replies do not imply any value for OpenEvent `recipients`.

Because every reply target must already exist, reply edges always point to a
`turn.single` or `turn.start` with an earlier global seq in the same channel
and cannot form cycles.

## 12. Application Responsibilities

The application, not `chat.v1`, is responsible for:

- generating `turn_id` for its own `turn.single` and `turn.start` events and
  keeping it unique under the same principal in the same channel;
- mapping principals to user or agent roles;
- channel visibility, membership changes, and authorization policy;
- recipients and event routing;
- interpreting `extensions` and ObjectKeys;
- choosing application behavior after a Chat publishing call reports an error.
  A direct OpenEvent producer follows the OpenEvent UUID contract; the Chat
  Python SDK obtains and passes UUIDs internally;
- business-level idempotency and deduplication beyond OpenEvent's rejection of
  an already consumed message UUID;
- choosing behavior for malformed JSON, unknown kinds, missing references,
  duplicate single/start creations, broken chains, and forks;
- rendering concurrent turns and cancellation state; when using the Chat SDK,
  observing effective `turn.cancel` events through its subscription callback,
  or reading events by seq in a direct protocol implementation, and stopping
  the corresponding model, tool, or other application work. The Chat protocol
  and SDK record cancellation facts but do not directly interrupt application
  tasks.

A TurnRef identifies one logical turn. The OpenEvent UUID identifies one
underlying message only: a duplicate UUID is rejected without comparing its
payload, channel, principal, or other fields. It does not create a Chat
operation ID or establish business-level idempotency. The protocol does not
scan or validate UUID consumption while reconstructing turns.

## 13. Security And Persistence

- Channel ACL, not recipients, is the confidentiality boundary.
- A newly added channel member may be able to read the entire retained history.
- Removing a member cannot revoke data or ObjectKeys already obtained.
- ObjectKeys are bearer capabilities and may be permanently transferable.
- Any principal with channel write permission can cancel any existing TurnRef.
- Chat events are append-only. The protocol defines no edit, delete, redaction,
  or retention operation.

Applications must not place hidden reasoning, credentials, private agent state,
or other data that participants must not read into payloads, extensions, or
attached objects.
