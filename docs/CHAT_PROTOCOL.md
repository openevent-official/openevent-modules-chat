# Chat Protocol chat.v1

[中文版本](CHAT_PROTOCOL_cn.md)

> Status: public protocol specification
> Scope: event payloads in OpenEvent Channels with `protocol="chat.v1"`

## 1. Protocol Boundary

`chat.v1` is a low-level event protocol for permanent conversations between users and Agents. One OpenEvent Channel represents one permanent conversation
and may contain multiple user principals and multiple Agent principals.

Channel history does not identify which principals are users or Agents. Role mapping is an application responsibility outside the protocol. The protocol records
participant-visible interaction events and submission-number reservation control events; Agent internal state, hidden reasoning, model requests, tool internals,
secrets, and debug logs are outside its scope.

The protocol defines six turn events and one submission-number reservation control event:

- `turn.single`
- `turn.start`
- `turn.append`
- `turn.reset`
- `turn.end`
- `turn.cancel`
- `submission.reserve`

Other event kinds do not belong to `chat.v1`. Applications may attach participant-visible metadata through the optional `extensions` object, but extensions
must not change base turn semantics.

OpenEvent does not parse JSON payloads. This document defines protocol conformance without requiring applications to reject, skip, display, or stop processing
invalid or conflicting history in any particular way.

This specification uses `reply_to_seqs`; the old `reply_to_turns` field is not part of it. The original `chat.v1` name remains in use. There is no dual reading
of old fields, automatic conversion, or migration of old history.

The protocol includes `turn.reset` and the empty-content writing rules in Section 4; Section 15 defines reset completely.
Channels continue to use `chat.v1`, and existing valid history retains its original meaning. Before sending events using these extensions, operators must ensure
that the relevant SDKs, backends, browsers, and other readers support this specification. This specification provides no capability negotiation and does not
guarantee that older readers can process the new kind or the new empty-content conditions.

## 2. Channels and OpenEvent Fields

Every Chat Channel must set:

```text
ChannelInfo.protocol = "chat.v1"
```

The payload contains no version field. `ChannelInfo.description` has no `chat.v1` schema and may contain application-defined text.

The protocol imposes no additional restrictions on Channel visibility or membership. Members may change dynamically. OpenEvent's current ACL determines
read and write permissions for each event.

OpenEvent top-level fields retain their native semantics:

- `EventMessage.seq` is the authoritative global event order and the event position referenced by `pre_seq` and `reply_to_seqs`; it is not `turn_id`.
- `EventMessage.principal` is the publisher. The publisher of `turn.single` or `turn.start` owns the turn; append/reset/end must be published by that owner.
- `EventMessage.ts_ms` is the server receive time.
- `EventMessage.uuid` is the nonzero UUID assigned by OpenEvent to this committed message. It is not `turn_id`, TurnRef, creation-event seq, `pre_seq`,
  a reply reference, or a payload field. UUID allocation, consumption, duplicate rejection, and uncertain outcomes are defined solely by the OpenEvent API contract.
- `EventMessage.recipients` may carry application semantics freely within OpenEvent server-side publish validation rules.
  `chat.v1` does not require it to be empty, constant within a turn, or consistent with reply relationships.
- Turn events may have nonempty `EventMessage.object_keys`; object contents and their presentation meaning are application-defined. The base protocol does not
  associate an ObjectKey with a particular content part. Section 15 defines how reset replaces the current attachment set without changing ObjectKey access
  or retention semantics. `submission.reserve` must have empty `object_keys`.

OpenEvent recipients are a filter, not an ACL. Readers that need to reconstruct a complete conversation should read the Channel with
`only_my_recipient=false`.

## 3. JSON, TurnRef, and Common Fields

Every `chat.v1` payload must be a UTF-8 JSON object.

Common fields:

| Field | Rule |
| --- | --- |
| `kind` | Required string: only `turn.single`, `turn.start`, `turn.append`, `turn.reset`, `turn.end`, `turn.cancel`, or `submission.reserve` |
| `turn_id` | Required for `turn.single`, `turn.start`, `turn.append`, `turn.reset`, and `turn.end`; must be absent for `turn.cancel` and `submission.reserve` |
| `extensions` | Optional JSON object interpreted only by the application |

`turn_id` must be a nonempty string whose UTF-8 encoding is no more than 128 bytes. Producers and consumers compare exact strings without trimming,
changing case, or performing Unicode normalization.

A turn is uniquely identified within its Channel by the following TurnRef:

```json
{
  "principal": 9001,
  "turn_id": "turn-01JABC"
}
```

TurnRef must be a JSON object containing only these fields:

- `principal`: the OpenEvent top-level principal of the `turn.single` or `turn.start` that created the turn, using the OpenEvent principal `uint64` JSON integer rules;
- `turn_id`: a string satisfying the rules above.

TurnRef uses exact `(principal, turn_id)` comparison. The event location already determines the Channel, so protocol descriptions use TurnRef, while the complete
storage index is `(channel_id, principal, turn_id)`. Writers must not create two turns with the same `turn_id` under the same principal in the same Channel.
Different principals may use the same `turn_id`; those are different turns.

Payload positions that reference OpenEvent seq include `pre_seq` and each element of `reply_to_seqs`. All use JSON integers in the range
`1..18446744073709551615`, a nonzero `uint64`; strings, booleans, and fractional numbers are not allowed. `pre_seq` points to the preceding node in a streaming
turn chain. Section 11 defines reply targets and reply semantics.

Unknown top-level payload fields are not part of `chat.v1`. Application data must be placed in `extensions`. Extension values may be any JSON value, but must
remain participant-visible metadata and must not redefine `kind`, `turn_id`, `pre_seq`, `reply_to_seqs`, `reserved_through`, TurnRef, reply, content, terminal-state,
or number-reservation semantics.

The OpenEvent deployment controls the payload size limit; `chat.v1` does not define a second byte limit.

## 4. Content Part

The first version supports only text parts:

```json
{
  "type": "text",
  "text": "你好"
}
```

`content` must be a JSON array. The conditions for empty arrays are defined here for each event:

| Event | Conditions for an empty array |
| --- | --- |
| `turn.single` | May be empty to support an application-defined turn containing only object attachments. |
| `turn.start` | May be empty, including when top-level `object_keys` is also empty; it still creates a cancellable streaming turn and does not complete it. |
| `turn.append` | May be empty only when this event has nonempty top-level `object_keys`, to append attachments alone; an event with both empty `content` and empty `object_keys` violates the protocol. |
| `turn.reset` | May be empty; Section 15 defines clearing and replacement. |

Each array item must be an object containing only these fields:

- `type`: the fixed string `text`;
- `text`: a nonempty string.

Part order matters. Applications use the part order in `turn.single` directly. For streaming turns, they reconstruct content along the valid turn chain,
preserving each event's part order; a reset replaces previously aggregated content as specified in Section 15.
Chunk boundaries have no protocol meaning: applications may append characters, words, sentences, or larger text fragments.

The protocol does not define binary, image, audio, tool-call, or other content parts. Applications may attach ObjectKeys to the OpenEvent message carrying an
event, but this does not introduce a new `chat.v1` content type.

## 5. `turn.single`

`turn.single` creates and normally completes a non-appendable turn in one event, suitable for a user submitting a complete input at once. The writer generates
`turn_id` before publishing; the OpenEvent top-level principal and payload `turn_id` together form the turn's TurnRef.

```json
{
  "kind": "turn.single",
  "turn_id": "turn-user-1",
  "reply_to_seqs": [123400],
  "content": [
    {"type": "text", "text": "你好"}
  ]
}
```

Rules:

- `turn_id` must be present and satisfy the string rules in Section 3.
- Except for optional `extensions`, the payload must contain exactly `kind`, `turn_id`, `reply_to_seqs`, and `content`; therefore it must not contain
  `pre_seq` or `target_turn`.
- `reply_to_seqs` must satisfy Section 11.
- `content` must satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- Only one creation event may exist for the same owner principal and `turn_id` in a Channel. If an earlier `turn.single` or `turn.start` exists,
  another creation event of either kind is a protocol conflict.

`turn.single` permanently determines TurnRef, owner, `reply_to_seqs`, complete content, and the completed terminal state. It has no `pre_seq`; its OpenEvent
`seq` is simultaneously the turn's `creation_seq`, `start_seq`, and `terminal_seq`. A later `turn.end` is neither needed nor able to complete it again.

## 6. `turn.start`

`turn.start` creates a streaming turn that permits subsequent append and reset, and is that turn's first message. The writer generates `turn_id` before
publishing; the OpenEvent top-level principal and payload `turn_id` together form the turn's TurnRef.

```json
{
  "kind": "turn.start",
  "turn_id": "turn-user-1",
  "reply_to_seqs": [123400],
  "content": [
    {"type": "text", "text": "你好"}
  ]
}
```

Rules:

- `turn_id` must be present and satisfy the string rules in Section 3.
- Except for optional `extensions`, the payload must contain exactly `kind`, `turn_id`, `reply_to_seqs`, and `content`; therefore it must not contain
  `pre_seq` or `target_turn`.
- `reply_to_seqs` must satisfy Section 11.
- `content` must satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- Only one creation event may exist for the same owner principal and `turn_id` in a Channel. If an earlier `turn.single` or `turn.start` exists,
  another creation event of either kind is a protocol conflict.

`turn.start` permanently determines the turn's TurnRef, owner, and `reply_to_seqs`. It has no `pre_seq`; its OpenEvent `seq` is the turn's `creation_seq`
and `start_seq`, and the starting point for the subsequent append/reset/end chain.

## 7. `turn.append`

`turn.append` appends content to an existing turn owned by the publishing principal.

```json
{
  "kind": "turn.append",
  "turn_id": "turn-user-1",
  "pre_seq": 123402,
  "content": [
    {"type": "text", "text": "，世界"}
  ]
}
```

Rules:

- To affect turn state, the target TurnRef must have been created by an earlier `turn.start` in the same Channel and must not be terminal.
  A turn created by `turn.single` is already terminal, so later append events have no turn-state effect.
- For a nonterminal streaming turn, `pre_seq` must be present and equal the OpenEvent `seq` of its current valid chain tail: `turn.start`, `turn.append`, or `turn.reset`.
- Except for optional `extensions`, the payload must contain exactly `kind`, `turn_id`, `pre_seq`, and `content`; therefore it must not contain
  `reply_to_seqs` or `target_turn`.
- For a streaming turn on which the event has an effect, the OpenEvent top-level `principal` must equal the owner determined by `turn.start`.
- `content` must satisfy Section 4.

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

- To affect turn state, the target TurnRef must have been created by an earlier `turn.start` in the same Channel and must not be terminal.
  A turn created by `turn.single` is already completed, so later `turn.end` events have no turn-state effect.
- For a nonterminal streaming turn, `pre_seq` must be present and equal the OpenEvent `seq` of its current valid chain tail: `turn.start`, `turn.append`, or `turn.reset`.
- Except for optional `extensions`, the payload must contain exactly `kind`, `turn_id`, and `pre_seq`; therefore it must not contain
  `content`, `reply_to_seqs`, or `target_turn`.
- The event records completion only; it does not repeat a final content snapshot.

## 9. `turn.cancel`

`turn.cancel` independently cancels an existing turn. Because the cancelling principal need not be the owner, the payload identifies the target with a full
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

- `target_turn` must be present and be a valid TurnRef, and its target must already have been created in the same Channel by an earlier `turn.single` or `turn.start`.
- Except for optional `extensions`, the payload must contain exactly `kind` and `target_turn`; therefore it must not contain top-level `turn_id`, `pre_seq`,
  `content`, or `reply_to_seqs`.
- Any principal permitted by OpenEvent to publish to the Channel may publish it. The publisher need not equal `target_turn.principal`, and the protocol
  need not know whether it is a user or Agent.
- Cancel is itself a terminal event and requires no later `turn.end` confirmation.

## 10. Chain and Terminal-State Semantics

Turns have two content and terminal-state forms:

- A turn created by `turn.single` has no append chain; the event supplies all content and normally completes the turn at the same seq.
- A streaming turn created by `turn.start` uses the following `pre_seq` single chain and is terminated by a later `turn.end` or `turn.cancel`.

For one TurnRef created by `turn.start`, the `turn.start`, valid `turn.append` and `turn.reset` events preceding the earliest terminal event, and the valid
`turn.end` when it is that earliest terminal event form a single chain through `pre_seq`:

- `turn.start` is the head and has no predecessor. Its OpenEvent `seq` is the first chain tail, not the turn ID.
- Each later append, reset, or normal end points to the current chain tail.
- Before the earliest terminal event, at most one append, reset, or end may use a given `pre_seq` as its predecessor. Multiple successors form a fork and violate the protocol.
- `turn.cancel` relates to the turn through `target_turn`, is not a chain successor, and never contains `pre_seq`.

The terminal event of `turn.single` is the event itself. For a turn created by `turn.start`, the terminal event is the correctly formatted `turn.end` or
`turn.cancel` for that TurnRef with the smallest OpenEvent global `seq`. Concurrent completion and cancellation therefore produce a deterministic result
shared by all readers.

Events are processed in ascending OpenEvent `seq` order. Once a TurnRef becomes terminal, later append, reset, end, or cancel events targeting it cannot change
its determined terminal state, final content, or preterminal chain tail. Such events have no turn-state effect even if committed. The protocol requires no
special parsing, field-extraction, or validation order for postterminal events; applications need not decide whether they are "valid" relative to the terminal
event. Only valid start/append/reset events preceding the earliest terminal `seq` participate in reconstructing a streaming turn's final content, with reset
replacement as specified in Section 15. A single turn's final content is fixed by `turn.single.content`.

Different TurnRefs may remain open and receive concurrent appends. Their events may interleave in the OpenEvent global order; each turn's `pre_seq` chain
determines its local content order.

## 11. Reply Relationships

`reply_to_seqs` references replied-to turns by their creation-event positions. It expresses causal context, not routing or authorization. Complete reply-field rules:

- `turn.single` and `turn.start` must contain `reply_to_seqs`, a JSON array that may be empty. Other events must not contain it.
- Elements must satisfy the nonzero `uint64` JSON integer rules in Section 3 and must not repeat within the array. The list preserves writer-supplied order and
  need not be sorted by seq. Its order adds no priority, routing, or execution-order semantics. An empty array means no protocol-level parent turn; multiple
  elements mean a response to multiple earlier turns.
- Each seq must equal the OpenEvent `EventMessage.seq` of an earlier committed `turn.single` or `turn.start` in the same Channel. It must not reference append,
  reset, end, cancel, `submission.reserve`, another Channel's message, the current creation event itself, or an uncommitted event.
- This document calls a turn's creation-event seq its `creation_seq`. It is derived directly from the top-level `EventMessage.seq` of `turn.single` or `turn.start`;
  no payload field is added. `turn_id` and the full TurnRef still identify the turn. The `pre_seq` of append/reset/end and the `target_turn` of cancel retain
  their own semantics; UUIDs are not reply references.
- A reference points to the whole turn, not a particular append, and does not freeze a text snapshot at reply publication. A target may still be streaming,
  completed, or cancelled. Its later valid appends or resets still belong to the same referenced turn. Applications that need a fixed snapshot must handle
  that outside the protocol.
- The creation event fixes the reply list, which cannot later change. The list implies no OpenEvent `recipients` value.

Writers must provide creation-event seq values satisfying these conditions and may directly use positions of observed creation messages. The protocol does not
require SDKs or applications to fetch, scan history, or maintain a separate index merely to verify reply targets. Readers may map creation-event seq to TurnRef
while reading history normally. SDK local validation and call behavior are defined by [`CHAT_SDK.md`](CHAT_SDK.md). Missing targets or invalid references
are still invalid history; Section 12 describes responsibility for handling them.

For example, in one Channel, seq `100` is turn A's start, seq `101` is A's append, and seq `110` is turn B's single. A new turn C may use
`"reply_to_seqs": [100, 110]`; `101` cannot be a reply target because it created no turn. The causal graph has edges `C → A` and `C → B`.
Every reply edge points to an earlier creation event, so the graph cannot contain a cycle.

## 12. Application Responsibilities

The following are application responsibilities, not responsibilities of `chat.v1`:

- Generate `turn_id` for published `turn.single` and `turn.start` events and guarantee uniqueness within the same Channel and principal.
- When using Section 14's number reservations, designate each Channel's number allocator and choose the mapping to `turn_id`, batch allocation, expiry, and query policies.
- Provide valid `reply_to_seqs` as specified in Section 11.
- Map principals to users or Agents.
- Choose Channel visibility, membership changes, and authorization policy.
- Define recipients and event routing.
- Interpret `extensions` and ObjectKeys.
- Decide when to reset a nonterminal streaming turn and how to present retries, progress, and historical audit information. Reset does not directly retry a model or undo tool execution.
- Decide application behavior after a Chat publish call returns an error. Producers publishing directly through OpenEvent follow its UUID contract;
  the Chat Python SDK obtains and passes UUIDs internally.
- Provide business-level idempotency and deduplication beyond OpenEvent's rejection of consumed message UUIDs.
- Decide how to handle invalid JSON, unknown kinds, missing references, duplicate single/start creation, broken chains, and forks.
- Display concurrent turns and cancellation state. Applications read events in seq order, schedule `fetch_page()` themselves when using the Chat SDK, observe
  effective `turn.cancel` events, and stop the corresponding model, tool, or other business processing. The Chat protocol and SDK record cancellation only;
  they do not directly terminate application tasks.

TurnRef identifies a logical turn. An OpenEvent UUID identifies one underlying message only: duplicate UUIDs are rejected, and the server does not compare the
payload, Channel, principal, or other fields. It creates no Chat operation ID and provides no business-level idempotency. Turn reconstruction does not scan for
or verify consumed UUIDs.

## 13. Security and Persistence

- Channel ACL is the confidentiality boundary; recipients are not.
- Newly joined Channel members may read all retained history.
- Removing members cannot revoke data or ObjectKeys they already obtained.
- An ObjectKey is a bearer capability that may be permanently transferred.
- Any principal with Channel write permission can cancel any existing TurnRef.
- Chat events are append-only. Reset changes only the current aggregate of a nonterminal turn; it neither rewrites nor deletes old events and does not revoke
  old ObjectKeys. The protocol defines no history editing, deletion, redaction, or retention-period operations.

Applications must not write hidden reasoning, credentials, private Agent state, or other data that participants should not read into payloads, extensions,
or attached objects.

## 14. `submission.reserve`

`submission.reserve` durably records an upper bound for reserved submission numbers in the current Channel. Applications can reserve a batch of
`submission_id` values at once and then issue numbers to senders from memory. Number spaces are isolated per Channel, so different Channels may use the
same number. A number is neither an OpenEvent `seq` nor a UUID; the reservation event's own `seq` also does not indicate that a user message has been submitted.

```json
{
  "kind": "submission.reserve",
  "reserved_through": 10000
}
```

Rules:

- Except for optional `extensions`, the payload must contain exactly `kind` and `reserved_through`. It must not contain `turn_id`, `content`,
  `reply_to_seqs`, `pre_seq`, or `target_turn`.
- `reserved_through` is the inclusive reservation upper bound, a JSON integer in `1..18446744073709551615`. Strings, booleans, and fractional numbers
  are not allowed. It is not a reference to an OpenEvent event position.
- OpenEvent top-level `object_keys` must be empty. Principal, recipients, ACL, and UUID follow the general rules in Section 2.
- This event neither creates nor modifies a turn, joins no `pre_seq` chain, produces no content or terminal state, and cannot be a reply target. Readers must
  recognize it as a valid control event and process its message position during normal history reading, rather than treating it as chat content or an unknown kind.

Applications using this capability must designate exactly one active number allocator per Channel at a time and guarantee that each number is issued once.
This restriction applies only to number allocation, not to the number of users, Agents, or turn writers participating in the Channel. The base protocol provides
no allocator election, deployment arbitration, or HTTP request deduplication.

Before issuing numbers, the allocator commits a reservation event covering them and confirms its successful OpenEvent commit. On recovery, the greatest
`reserved_through` among committed reservation records in that Channel is the reserved upper bound; new numbers must exceed it. With no reservations, allocation
may start at `1`. Unissued, unused, or failed-send numbers may leave gaps and must not be reissued. Allocation must not wrap at the `uint64` maximum.

A reservation means "these numbers have been reserved", not "all messages in this batch have been written". An old timed-out reservation request may commit
later, so history may contain equal or smaller reservation upper bounds. Reading in seq order does not require strictly increasing `reserved_through` values,
and overlapping reservation declarations are not duplicate allocation. Recovery takes the maximum, not merely the last record. Number allocation order does
not determine user-message commit order either.

Applications define the mapping from numbers to `turn_id`, how many numbers the frontend obtains at a time, whether old batches expire, how long query results
are retained, and how a batch change waits for in-flight publishes. These choices do not change this section's reservation fields or the turn state machine.

## 15. `turn.reset`

`turn.reset` replaces the current text and attachment set of a nonterminal streaming turn owned by the publishing principal, while allowing the same turn to
continue streaming. For example, after a model stream is interrupted, an application may retry and use the retry output to replace the current reply.
The application still decides when to retry and which model results to accept.

```json
{
  "kind": "turn.reset",
  "turn_id": "turn-agent-1",
  "pre_seq": 123403,
  "content": []
}
```

Field and chain rules:

- Except for optional `extensions`, the payload must contain exactly `kind`, `turn_id`, `pre_seq`, and `content`; it must not contain `reply_to_seqs` or
  `target_turn`. `turn_id` and `pre_seq` satisfy Section 3, and `content` satisfies Section 4.
- To affect state, the target TurnRef must have been created by an earlier `turn.start` in the same Channel and must not be terminal. The OpenEvent top-level
  `principal` must equal that owner. Reset creates no turn and cannot modify another owner's turn.
- `pre_seq` must equal the turn's current valid chain tail: the seq of its latest valid `turn.start`, `turn.append`, or `turn.reset`. Once effective, reset's own
  seq becomes the new chain tail, and a following append, reset, or end connects to that new tail.
- Reset does not bypass old writes. Before submitting reset at the correct chain tail, writers must resolve old chain writes and ensure no pending publish
  targeting the old chain tail can still arrive. A reset and an old append using the same predecessor form a fork forbidden by Section 10. Merely failing to
  find an old message does not justify disregarding a request that may still commit.
- Reset is not terminal. Consecutive resets are allowed, as is an end directly after reset; an append after reset is not required. A completed or cancelled turn,
  including a single turn, is not reopened by reset. Section 10 governs all postterminal handling.

Current-content replacement rules:

- When processing an effective reset in seq order, discard the text previously aggregated for that turn and use reset's `content` as the new text-part list.
  An empty array clears the text; a nonempty array replaces it immediately with those parts. Later valid appends add to this new list.
- Also clear the attachment set previously aggregated for that turn and use the reset event's `EventMessage.object_keys` as the starting point of the new set.
  An empty list clears attachments. ObjectKeys that should remain may be referenced again in the new list. Applications continue to interpret the object content
  and presentation of later events, but must not reincorporate attachments from events preceding reset into the current set. `object_keys` remains an OpenEvent
  top-level field; no payload field with that name is added.
- `content` and top-level `object_keys` may both be empty. The turn still exists and remains streaming; it simply has no current text or attachments.
- Reset does not change owner, TurnRef, `creation_seq`, `start_seq`, or the creation event's `reply_to_seqs`. Ordering and reference positions based on the
  creation seq remain unchanged. Replies to this turn still reference its original creation event; reset's seq cannot become a new reply target.
- Old start, append, and reset events and their object references remain in immutable history. Reset deletes no objects, revokes no access, and undoes no
  application side effects that have already occurred. Live reading and replay from the beginning must derive the same current text, attachment boundary,
  chain tail, and terminal state from the same event prefix.

For example, all the following events belong to the same TurnRef:

| seq | Event | `pre_seq` | Current text | Current state |
| --- | --- | --- | --- | --- |
| 100 | start, content is “旧” | None | 旧 | Streaming |
| 101 | append, content is “回答” | 100 | 旧回答 | Streaming |
| 105 | reset, content is “新” | 101 | 新 | Streaming |
| 106 | append, content is “回答” | 105 | 新回答 | Streaming |
| 108 | end | 106 | 新回答 | completed |

If cancel already took effect at seq 104, the reset and later events in this table do not change the turn. Its final text remains “旧回答” and its state is cancelled.
