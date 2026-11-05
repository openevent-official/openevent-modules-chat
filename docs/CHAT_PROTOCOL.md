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

This specification retains the `chat.v1` name but is incompatible with older creation-event reply references, ends without content manifests, and cancels without creation positions. The old `reply_to_turns` field is also excluded.
There is no dual reading, automatic conversion, migration of old history, or capability negotiation. All SDKs, backends, browsers, and other readers and writers in a deployment must adopt this specification together.
Sections 8, 9, and 12 respectively define streaming reset, end manifests, and reply targets.

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
- Turn events other than `turn.end` may have nonempty `EventMessage.object_keys`; object contents and their presentation meaning are application-defined. The base protocol does not
  associate an ObjectKey with a particular content part. Section 8 defines how reset replaces the current attachment set without changing ObjectKey access
  or retention semantics. `turn.end` and `submission.reserve` must have empty `object_keys`.

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

Payload positions that reference OpenEvent seq include `start_seq`, `pre_seq`, and each element of `content_seqs` and `reply_to_seqs`. All use JSON integers in the range
`1..18446744073709551615`, a nonzero `uint64`; strings, booleans, and fractional numbers are not allowed. `pre_seq` points to the preceding node in a streaming
turn chain. Section 12 defines reply targets and reply semantics.

Unknown top-level payload fields are not part of `chat.v1`. Application data must be placed in `extensions`. Extension values may be any JSON value, but must
remain participant-visible metadata and must not redefine `kind`, `turn_id`, `start_seq`, `pre_seq`, `content_seqs`, `error`, `reply_to_seqs`, `reserved_through`, TurnRef, reply, content, terminal-state,
or number-reservation semantics.
Base readers need only establish that extensions form a valid JSON object; they need not convert its values into their language's numbers or objects. Forwarding
must preserve extension data, whose interpretation belongs to the application.

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
| `turn.reset` | May be empty; Section 8 defines clearing and replacement. |

Each array item must be an object containing only these fields:

- `type`: the fixed string `text`;
- `text`: a nonempty string.

Part order matters. Applications use the part order in `turn.single` directly. For streaming turns, they reconstruct content along the valid turn chain,
preserving each event's part order; a reset replaces previously aggregated content as specified in Section 8.
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
- `reply_to_seqs` must satisfy Section 12.
- `content` must satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- Only one creation event may exist for the same owner principal and `turn_id` in a Channel. If an earlier `turn.single` or `turn.start` exists,
  another creation event of either kind is a protocol conflict.

`turn.single` permanently determines TurnRef, owner, `reply_to_seqs`, complete content, and the completed terminal state. It has no `pre_seq`; its OpenEvent
`seq` is simultaneously the turn's `start_seq` and `terminal_seq`. A later `turn.end` is neither needed nor able to complete it again.

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
- `reply_to_seqs` must satisfy Section 12.
- `content` must satisfy Section 4.
- The OpenEvent top-level `principal` becomes the turn owner.
- Only one creation event may exist for the same owner principal and `turn_id` in a Channel. If an earlier `turn.single` or `turn.start` exists,
  another creation event of either kind is a protocol conflict.

`turn.start` permanently determines the turn's TurnRef, owner, and `reply_to_seqs`. It has no `pre_seq`; its OpenEvent `seq` is the turn's `start_seq`
and the starting point for the subsequent append/reset/end chain.

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

## 8. `turn.reset`

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
  targeting the old chain tail can still arrive. A reset and an old append using the same predecessor form a fork forbidden by Section 11. Merely failing to
  find an old message does not justify disregarding a request that may still commit.
- Reset is not terminal. Consecutive resets are allowed, as is an end directly after reset; an append after reset is not required. A completed or cancelled turn,
  including a single turn, is not reopened by reset. Section 11 governs all postterminal handling.

Current-content replacement rules:

- When processing an effective reset in seq order, discard the text previously aggregated for that turn and use reset's `content` as the new text-part list.
  An empty array clears the text; a nonempty array replaces it immediately with those parts. Later valid appends add to this new list.
- Also clear the attachment set previously aggregated for that turn and use the reset event's `EventMessage.object_keys` as the starting point of the new set.
  An empty list clears attachments. ObjectKeys that should remain may be referenced again in the new list. Applications continue to interpret the object content
  and presentation of later events, but must not reincorporate attachments from events preceding reset into the current set. `object_keys` remains an OpenEvent
  top-level field; no payload field with that name is added.
- `content` and top-level `object_keys` may both be empty. The turn still exists and remains streaming; it simply has no current text or attachments.
- Reset does not change owner, TurnRef, `start_seq`, or the creation event's `reply_to_seqs`, nor ordering by creation position.
  A reset seq is not a reply target; Section 12 defines reply targets after completion.
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

## 9. `turn.end`

`turn.end` completes a streaming turn owned by the publishing principal. A normal end records effective content-event positions, without repeating the body or attachments:

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "start_seq": 123401,
  "pre_seq": 123403,
  "content_seqs": [123401, 123402, 123403]
}
```

Rules:

- Apart from optional `extensions`, the common required fields are `kind`, `turn_id`, `start_seq`, and `pre_seq`, plus exactly one of
  `content_seqs` or `error`. Neither `content`, `reply_to_seqs`, nor `target_turn` is allowed, and top-level `object_keys` must be empty.
- To affect state, the target must be an earlier, nonterminal streaming turn in the same Channel, and the publisher must be its owner.
  `start_seq` must equal its `turn.start.seq`; `pre_seq` must equal the current effective start, append, or reset chain tail.
- A normal end's `content_seqs` must be a nonempty, strictly increasing array of nonzero uint64 JSON integers.
  Every entry satisfies `start_seq <= seq <= pre_seq`, and the final entry must equal `pre_seq`.
- The list must exactly identify all events contributing current content: start and all subsequent effective appends if there was no effective reset,
  or the last effective reset and all subsequent effective appends otherwise. Events replaced by reset are excluded; an empty start or reset must still appear,
  even when it has neither content nor attachments. Listed events must belong to this TurnRef and follow its effective chain, with no missing or extra events.
  A first entry naming start equals `start_seq`; otherwise it must name this turn's effective reset.
- There is no separate item-count limit; the complete payload remains subject to OpenEvent's size limit. The
  [SDK writer contract](CHAT_SDK.md#51-independent-writers) defines handling when a normal end manifest is too large.

When an oversized normal end manifest cannot be committed, a small error end can be used:

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "start_seq": 123401,
  "pre_seq": 123403,
  "error": "turn_too_long"
}
```

The only allowed `error` value is the string `turn_too_long`; `content_seqs` must be absent. This end also enters completed and may be replied to.
Its final body and attachments are empty; readers must not continue presenting previously accumulated partial content as its final content. Historical events and objects remain.
Error ends follow the same owner, chain-tail, and earliest-terminal rules as normal ends.

## 10. `turn.cancel`

`turn.cancel` independently terminates an existing turn without waiting for its owner to send end. Since the canceller need not be the owner, it carries both the full target identity and its creation position:

```json
{
  "kind": "turn.cancel",
  "target_turn": {
    "principal": 9002,
    "turn_id": "turn-agent-1"
  },
  "start_seq": 123401
}
```

- Apart from optional `extensions`, the payload must contain exactly `kind`, `target_turn`, and `start_seq`.
  It contains no `pre_seq`, `content`, `content_seqs`, `error`, or `reply_to_seqs`.
- `target_turn` must be a valid TurnRef, and `start_seq` must equal that target's earlier single/start creation-event seq in the same Channel.
- Any principal permitted to publish to the Channel may cancel; it need not be the owner, and no user or Agent role is required.
- Cancel contains no effective-content manifest and participates immediately in Section 11's earliest-terminal decision. When effective, it retains prior effective content and includes its own top-level attachments.
  A cancel targeting an already terminal turn has no state effect.

## 11. Chain and Terminal-State Semantics

A `turn.single` is both its creation event and completed terminal event. A streaming turn's start, effective appends/resets, and effective end form a single `pre_seq` chain:

- Start is the head, without a predecessor, and its seq is the first chain tail.
- Each subsequent append, reset, or end points to the current tail; before the earliest terminal event, a predecessor cannot have multiple successors.
- Cancel identifies the target using `target_turn` and `start_seq`; it is not a chain successor and has no `pre_seq`.

A streaming turn's effective terminal event is its end/cancel with the smallest OpenEvent global seq: an effective end means completed, and an effective cancel means cancelled.
Concurrent ends and cancels therefore yield the same outcome for every reader. `terminal_seq` is that effective terminal-event position, not the creation or last-content position;
it is derived and adds no same-named payload field.

Process events in ascending seq order. After termination, later append/reset/end/cancel events cannot change the terminal state, final content, attachments, or preterminal tail, even if committed.
No additional parsing or validation order is required for such late events, and applications need not assess their dynamic validity against the terminal state.
Effective preterminal start/append/reset events aggregate content under Section 8. A normal end's manifest must match that aggregate; an error end clears the final body and attachments under Section 9.
Cancel retains the aggregate at cancellation plus its own attachments. A single turn retains its own body and attachments.

Different turns may remain open concurrently and interleave in the global sequence; each chain determines its local content order.

## 12. Reply Relationships

`reply_to_seqs` references the effective normal-completion positions of replied-to turns, expressing causal context rather than routing or authorization:

- Only single/start must contain this JSON array; it may be empty. Elements are nonzero uint64 JSON integers under Section 3, without duplicates.
  Writer order is preserved, sorting is not required, and order adds no priority.
- Each entry must be an earlier committed single or end seq in the same Channel that actually became a completed terminal event.
  Start, append, reset, cancel, reservations, messages in other Channels, the current event, and uncommitted events are not valid targets.
  An earlier effective cancel disqualifies a later end as a reply target.
- Cancelled turns cannot be replied to. An effective end with `error="turn_too_long"` is completed and may be replied to.
- A reference names the whole completed turn. Its creation event fixes its original reply list permanently; reply relationships imply no OpenEvent recipients.
- `start_seq` is the single/start position used to locate creation information; `terminal_seq` is the reply target.
  These are equal for single and different for streaming turns. TurnRef still identifies the turn; UUIDs are not reply references.

Writers supply terminal positions satisfying these conditions. The protocol does not require extra SDK history reads when publishing replies; normal history reading can map completed terminal positions to turns.
To read content by a target terminal position, use the [SDK targeted-read contract](CHAT_SDK.md#33-read-content-by-terminal-position); it does not replace the valid-target precondition.

For example, A starts at 100, appends at 101, and has an effective end at 102; B is a single at 110. New turn C at 111 may reference
`[102, 110]`, but neither 100 nor 101. If A was cancelled at 102 instead, it cannot be replied to. Every reply edge points to a normal terminal event preceding the current creation event,
so the causal graph cannot contain a cycle.

## 13. `submission.reserve`

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

## 14. Application Responsibilities

The following are application responsibilities, not responsibilities of `chat.v1`:

- Generate `turn_id` for published `turn.single` and `turn.start` events and guarantee uniqueness within the same Channel and principal.
- When using Section 13's number reservations, designate each Channel's number allocator and choose the mapping to `turn_id`, batch allocation, expiry, and query policies.
- Provide valid `reply_to_seqs` as specified in Section 12.
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

## 15. Security and Persistence

- Channel ACL is the confidentiality boundary; recipients are not.
- Newly joined Channel members may read all retained history.
- Removing members cannot revoke data or ObjectKeys they already obtained.
- An ObjectKey is a bearer capability that may be permanently transferred.
- Any principal with Channel write permission can cancel any existing TurnRef.
- Chat events are append-only. Reset changes only the current aggregate of a nonterminal turn; it neither rewrites nor deletes old events and does not revoke
  old ObjectKeys. The protocol defines no history editing, deletion, redaction, or retention-period operations.

Applications must not write hidden reasoning, credentials, private Agent state, or other data that participants should not read into payloads, extensions,
or attached objects.
