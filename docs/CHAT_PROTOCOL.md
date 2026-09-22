# Chat Protocol chat.v1

[中文版](CHAT_PROTOCOL_cn.md)

> Status: current protocol specification, implemented by the SDK
> Scope: event payloads for OpenEvent channels with `protocol="chat.v1"`

## 1. Protocol Boundary

`chat.v1` is a low-level event protocol for persistent conversations between
users and agents. One OpenEvent channel represents one permanent conversation
and may contain multiple user principals and multiple agent principals.

The channel history does not identify which principals are users or agents.
Role mapping is an application concern outside this protocol. The protocol
records participant-visible interaction events and submission reservation control events; agent internals, hidden
reasoning, model requests, tool internals, secrets, and debug logs are outside
its scope.

The protocol defines five turn event kinds and one submission reservation control event kind:

- `turn.single`
- `turn.start`
- `turn.append`
- `turn.end`
- `turn.cancel`
- `submission.reserve`

Additional event kinds are not part of `chat.v1`. Applications may attach
visible metadata through the optional `extensions` object, but extensions do
not change the base turn semantics.

OpenEvent does not parse the JSON payload. This specification defines whether
an event is well-formed, but does not require applications to reject, skip,
display, or stop on malformed or conflicting history.

This specification uses `reply_to_seqs`; the old `reply_to_turns` field
is not part of it. It retains the initial `chat.v1` name without dual reading,
automatic conversion, or migration of old history.

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
  position referenced by `pre_seq` and `reply_to_seqs`; it is not `turn_id`.
- `EventMessage.principal` is the publisher. A `turn.single` or `turn.start`
  publisher is the turn owner, and append/end publishers MUST equal that owner.
- `EventMessage.ts_ms` is the server receive time.
- `EventMessage.uuid` is the nonzero UUID allocated by OpenEvent for this one
  committed message. It is not a `turn_id`, TurnRef, creation-event seq,
  `pre_seq`, reply reference, or payload field. Allocation, consumption,
  duplicate rejection, and uncertain-result
  handling are defined only by the OpenEvent API contract.
- `EventMessage.recipients` is freely usable for application-defined semantics.
  Its values still have to satisfy the OpenEvent server's publish validation;
  `chat.v1` does not require the list to be empty, stable within a turn, or
  related to replies.
- Turn events MAY include `EventMessage.object_keys`. Their meaning is application-
  defined and the base protocol does not associate them with a content part.
  For `submission.reserve`, `object_keys` MUST be empty.

OpenEvent recipients are a filtering field, not an ACL. A reader that needs to
reconstruct the complete conversation SHOULD read the channel with
`only_my_recipient=false`.

## 3. JSON, TurnRef, And Common Fields

Every `chat.v1` payload MUST be a UTF-8 JSON object.

Common fields:

| Field | Rule |
| --- | --- |
| `kind` | Required string; exactly `turn.single`, `turn.start`, `turn.append`, `turn.end`, `turn.cancel`, or `submission.reserve` |
| `turn_id` | Required for `turn.single`, `turn.start`, `turn.append`, and `turn.end`; MUST be absent from `turn.cancel` and `submission.reserve` |
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

Payload values referencing OpenEvent sequences are `pre_seq` and each element
of `reply_to_seqs`. Each uses a nonzero `uint64` JSON integer in
`1..18446744073709551615`, not a string, boolean, or fraction. `pre_seq` names
the previous node in a streaming turn's chain; Section 11 defines reply targets
and semantics for `reply_to_seqs`.

Unknown top-level payload fields are not part of `chat.v1`. Application data
MUST be placed under `extensions`. Extension values may be any JSON value, but
must remain participant-visible metadata and must not redefine
`kind`, `turn_id`, `pre_seq`, `reply_to_seqs`, `reserved_through`, TurnRef,
reply, content, terminal, or submission reservation semantics.

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
  "reply_to_seqs": [123400],
  "content": [
    {"type": "text", "text": "Hello"}
  ]
}
```

Rules:

- `turn_id` MUST be present and satisfy Section 3.
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, `reply_to_seqs`, and `content`. Therefore `pre_seq` and
  `target_turn` MUST be absent.
- `reply_to_seqs` MUST satisfy Section 11.
- `content` MUST satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- One channel permits only one creation event for a given owner principal and
  `turn_id`. If an earlier `turn.single` or `turn.start` exists, another event
  of either creation kind is a protocol conflict.

`turn.single` permanently fixes the TurnRef, owner, `reply_to_seqs`, complete
content, and completed terminal state. It has no `pre_seq`. Its
OpenEvent `seq` is the turn's `creation_seq`, `start_seq`, and `terminal_seq`.
No later `turn.end` is needed or can complete it again.

## 6. `turn.start`

`turn.start` creates a streaming turn that permits later append events and is
the turn's first event. The writer generates `turn_id` before publication. The
top-level OpenEvent principal and payload `turn_id` together form the turn's
TurnRef.

```json
{
  "kind": "turn.start",
  "turn_id": "turn-user-1",
  "reply_to_seqs": [123400],
  "content": [
    {"type": "text", "text": "Hello"}
  ]
}
```

Rules:

- `turn_id` MUST be present and satisfy Section 3.
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, `reply_to_seqs`, and `content`. Therefore `pre_seq` and
  `target_turn` MUST be absent.
- `reply_to_seqs` MUST satisfy Section 11.
- `content` MUST satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- One channel permits only one creation event for a given owner principal and
  `turn_id`. If an earlier `turn.single` or `turn.start` exists, another event
  of either creation kind is a protocol conflict.

`turn.start` fixes the turn's TurnRef, owner, and `reply_to_seqs` for its entire
lifetime. It has no `pre_seq`; its OpenEvent `seq` is the turn's `creation_seq`
and `start_seq`, and the starting point of the subsequent append/end chain.

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
  `turn_id`, `pre_seq`, and `content`. Therefore `reply_to_seqs` and
  `target_turn` MUST be absent.
- For a streaming turn on which the event has a state effect, the top-level
  principal MUST equal the owner established by `turn.start`.
- `content` MUST satisfy Section 4.

## 8. `turn.end`

`turn.end` normally completes a turn owned by the publishing principal.

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "pre_seq": 123403
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
- Other than optional `extensions`, the payload MUST contain exactly `kind`,
  `turn_id`, and `pre_seq`. Therefore `content`, `reply_to_seqs`, and
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
  `target_turn`. Therefore top-level `turn_id`, `pre_seq`, `content`,
  and `reply_to_seqs` MUST be absent.
- Any principal allowed by OpenEvent to publish to the channel MAY publish the
  event. It need not equal `target_turn.principal`, and the protocol does not
  identify user or agent roles.
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

`reply_to_seqs` references turns by their creation-event positions and records
causal context, not routing or authorization. The complete reply-field rules
are:

- `turn.single` and `turn.start` MUST contain `reply_to_seqs` as a JSON array,
  which MAY be empty. Other events MUST NOT contain it.
- Each array element MUST satisfy the nonzero `uint64` JSON integer rules in
  Section 3, and elements MUST be unique. The list preserves the writer's
  order; it need not be sorted by seq. Order alone adds no priority, routing,
  or execution-order semantics. An empty array means no protocol-level parent;
  multiple elements mean a reply to several earlier turns.
- Each seq MUST equal the OpenEvent `EventMessage.seq` of an earlier committed
  `turn.single` or `turn.start` in the same channel. An append, end, cancel, `submission.reserve`,
  message from another channel, the current creation event itself, or an
  uncommitted event MUST NOT be referenced.
- This document calls a turn's creation-event seq its `creation_seq`. It is
  derived directly from the top-level `EventMessage.seq` of `turn.single` or
  `turn.start`, not added as another payload field. `turn_id` and the complete
  TurnRef still identify turns; append/end `pre_seq` and cancel `target_turn`
  retain their respective meanings. UUIDs are not reply references.
- A reference addresses the whole turn, not one of its appends, and does not
  freeze a text snapshot at the time the reply is published. The referenced
  turn MAY still be open, completed, or cancelled. Its later valid appends
  remain part of that same referenced turn. An application that needs to fix
  the content it saw at that time handles this outside the protocol.
- The creation event fixes the reply list, which cannot be modified later.
  The list implies no value for OpenEvent `recipients`.

The writer is responsible for supplying creation-event seqs that satisfy
these conditions and may use positions from creation messages it has already
observed. The protocol does not require an SDK or application to issue extra
Fetch calls, scan history, or maintain an additional index to validate reply
targets. A reader may associate creation-event seqs with TurnRefs during its
normal history reading. SDK local validation and call behavior are defined in
[`CHAT_SDK.md`](CHAT_SDK.md). Missing targets and invalid references remain
invalid history; Section 12 covers application handling policy.

For example, in one channel, seq `100` is turn A's start, seq `101` is A's
append, and seq `110` is turn B's single. A new turn C may use
`"reply_to_seqs": [100, 110]`; `101` is not a reply target because it did not
create a turn. The two causal edges are `C → A` and `C → B`. Every reply edge
points to an earlier creation event, so cycles cannot form.

## 12. Application Responsibilities

The application, not `chat.v1`, is responsible for:

- generating `turn_id` for its own `turn.single` and `turn.start` events and
  keeping it unique under the same principal in the same channel;
- when using Section 14's reservation capability, designating each channel's
  submission allocator and choosing how identifiers map to `turn_id`, batch
  allocation, expiry, and lookup policies;
- supplying valid `reply_to_seqs` as defined in Section 11;
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
- rendering concurrent turns and cancellation state; reading events by seq,
  scheduling `fetch_page()` calls when using the Chat SDK, observing effective
  `turn.cancel` events, and stopping
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

## 14. `submission.reserve`

`submission.reserve` durably records the submission identifier reservation
ceiling in the current channel. An application can reserve a batch of
`submission_id` values once, then issue them to senders from memory. Each
channel has its own identifier space; different channels MAY use the same
identifiers. An identifier is neither an OpenEvent `seq` nor a UUID. The
reservation event's own `seq` does not confirm that any user message was
committed.

```json
{
  "kind": "submission.reserve",
  "reserved_through": 10000
}
```

Rules:

- Other than optional `extensions`, the payload MUST contain exactly `kind`
  and `reserved_through`. It MUST NOT contain `turn_id`, `content`,
  `reply_to_seqs`, `pre_seq`, or `target_turn`.
- `reserved_through` is the inclusive reservation ceiling. It MUST be a JSON
  integer in `1..18446744073709551615`, not a string, boolean, or fraction.
  It does not reference an OpenEvent event position.
- OpenEvent top-level `object_keys` MUST be empty. Principal, recipients, ACL,
  and UUID follow the general rules in Section 2.
- This event does not create or modify a turn, enter a `pre_seq` chain,
  contribute content or terminal state, or serve as a reply target. Readers
  MUST recognize it as a valid control event and process its message position
  during normal history reading. They SHOULD NOT treat it as chat content or
  an unknown kind.

An application using this capability is responsible for designating exactly
one submission allocator at a time per channel and issuing each identifier
only once. This restricts identifier allocation only, not the number of chat
users, agents, or turn writers in that channel. The base protocol provides no
allocator election, deployment arbitration, or HTTP request deduplication.

The allocator first commits a reservation event covering the identifiers it
intends to issue. It MUST receive confirmation of OpenEvent commitment before
issuing them. Recovery uses the largest `reserved_through` among committed
reservation records in that channel as the reserved ceiling. New identifiers
MUST exceed that ceiling; without any reservation record, allocation MAY
start at `1`. Identifiers that were never issued, never used, or used in a
failed send MAY leave gaps and MUST NOT be reissued. Allocation MUST NOT wrap
around at the `uint64` limit.

A reservation record means "these identifiers have been reserved," not "all
messages in this batch have been committed." An older reservation request
that timed out may commit later. Therefore history MAY contain equal or lower
reservation ceilings: when reading in `seq` order, `reserved_through` need not strictly increase,
and overlapping reservation declarations do not establish duplicate issuance.
Recovery takes the maximum rather than the last record. Identifier allocation
order does not determine user-message commitment order either.

Applications define the mapping to `turn_id`, frontend batch sizes, whether
older batches expire, result lookup retention, and how batch rotation waits
for publications already in progress. These choices do not alter this
section's reservation event fields or the turn state machine.
