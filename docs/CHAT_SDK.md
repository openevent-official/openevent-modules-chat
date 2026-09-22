# Chat Python SDK Contract

[中文版](CHAT_SDK_cn.md)

> Status: implemented public contract of the current SDK
> Scope: `openevent.chat_sdk` version 0.1.0

## 1. Scope And Requirements

The Chat Python SDK provides `chat.v1` writes, one-page Fetch reads, and
stateless message parsing. It does not provide a worker, agent runtime, UI, role mapping,
application authorization, business idempotency, or durable application
projections.

The SDK requires Python 3.10 or later, an installed `openevent-sdk>=0.8.1`,
and an existing readable `protocol="chat.v1"` channel. It uses the injected
public OpenEvent client and never installs an SDK from source or generates
protobuf modules.

`CHAT_PROTOCOL.md` defines payload fields and turn semantics. OpenEvent API
documentation defines authentication, ACL, Fetch, UUID, ObjectKey, and gRPC
status semantics. The current reference SDK is the installed `0.8.1`; its public
interface defines the Python call signatures.

## 2. Construction

```python
from openevent.sdk import OpenEventClient
from openevent.chat_sdk import create_client

events = OpenEventClient("127.0.0.1:50051", timeout_ms=1000.0)
chat = create_client(
    events,
    principal=9001,
    token="...",
    channel_id=10001,
    max_retries=3,
)
```

One `ChatProtocolClient` is permanently bound to one nonzero `principal`, one
token, and one nonzero `channel_id`.
Construction verifies that the channel is readable, has the requested ID,
and uses `protocol="chat.v1"`; it does not call Fetch, recover history, or start
a background thread before returning a ready client. The client keeps no per-turn
state table, writer registry, or strong references to writing objects.

Both the constructor and `create_client()` accept an optional `channel_validator`,
defaulting to `None`. Applications can supply a synchronous, read-only validator.
After the base checks, the SDK calls it once with the `ChannelInfo` returned by
that GetChannel request, without another channel read. Returning normally accepts
the channel; raising an exception fails construction with `ChannelInitializationError`
and `failure.category="contract"`. Validation is not retried, and the failure does
not include the original exception text. The callback defines application rules
such as membership and naming; the SDK does not hardcode them. Without a callback,
only the base checks run.

`max_retries` is a non-negative integer. `0` means one attempt. The history recovery
start is supplied to explicit `resume_turn()` calls, not construction. The injected
client must provide a positive finite `timeout_ms`, in milliseconds, and thread-safe
`get_channel`, `get_status`, `fetch`, `get_uuid`, `get_seq_by_uuid`, and
`publish_auto_seq` methods.

The SDK reads but never changes the injected client's `timeout_ms`, and it does not
close the injected client or its gRPC channel.

`parse_message(event_message)` is a public stateless parser. It strictly turns
one OpenEvent `EventMessage` into a `ParsedMessage` and validates that message's
`chat.v1` JSON and base fields. It does not maintain cross-event turn state,
perform Fetch, or decide application roles. Applications implementing bounded
history pagination should reuse it instead of maintaining a second payload
parser.

`ParsedMessage.payload` also supports `submission.reserve` control events,
distinguishes event types by `kind`, and preserves `reserved_through` as a Python
`int`. Field validity follows the [public protocol](CHAT_PROTOCOL.md). It has no
turn identity, so callers cannot assume every parsed message has a `turn_id`.

### 2.1 One Fetch per page

```python
page = chat.fetch_page(from_seq=8451, limit=100)
for message in page.messages:
    print(message.seq, message.payload)
print(page.next_seq, page.last_seq)
```

`fetch_page()` always filters to the bound Channel and sets
`only_my_recipient=false`. One call performs one logical Fetch and does not read
the next page automatically; retries of that same RPC still belong to this
logical call. The returned `FetchPage` contains `messages`, `next_seq`, and
`last_seq`. The SDK validates the Fetch paging contract and parses each message,
but does not save the page cursor or modify any writing object. The caller must pass
the returned `next_seq` as the next `from_seq`.
`messages` includes parsed `submission.reserve` control events with their
original seq values. The SDK does not remove an event from the page because it
creates no turn.

Applications that need continuous reads loop over `fetch_page()` themselves,
save `next_seq` after processing the page, and choose the wait at the tail and
when to stop. The SDK provides no subscription, message callback, scan-progress
wait, or background polling task and does not call OpenEvent `Subscribe`.

## 3. Publishing API

```python
from openevent.chat_sdk import ObjectKey, TextPart, TurnRef

user_creation_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("hello"),),
    recipients=(),
    object_keys=(ObjectKey(object_id=42, object_token="..."),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(TextPart("hello"),),
)
reply_creation_seq = writer.creation_seq
seq = writer.append(
    content=(TextPart("The report is ready."),),
    object_keys=(ObjectKey(object_id=43, object_token="..."),),
)
seq = writer.complete()
seq = chat.cancel_turn(target_turn=TurnRef(9002, "other-turn"))
```

Every successful publishing call confirms that OpenEvent committed its event;
success does not mean an application projection or browser has observed it.
`start_turn()` returns a publicly exported `TurnWriter` whose `creation_seq` is
the creation-event position; `single_turn()` returns the creation-event seq
directly. Both positions can be used in a later reply's `reply_to_seqs`.
`writer.append()`, `writer.complete()`, `cancel_turn()`, and
`reserve_submissions()` return the committed seq of their respective events.

- `single_turn` creates a completed turn. Its `content` may be empty.
- `start_turn` creates a streaming turn and requires non-empty `content`;
  it returns a writing object bound to that turn only after `turn.start` is
  confirmed committed.
- `writer.append` adds content and `writer.complete` completes the turn. They
  address only the bound streaming turn, accept no `turn_id`, and never
  automatically read history.
- `cancel_turn` accepts a complete `TurnRef`; the target owner may differ from
  this client principal.
- `single_turn`, `start_turn`, and `writer.append` accept
  `object_keys: Iterable[ObjectKey] = ()`. Attachments are written only to the
  current published event's top-level OpenEvent `object_keys`; they are neither
  inherited from earlier events nor written back to the creation event.
  A `writer.append` with attachments still requires protocol-valid non-empty `content`.
- `reply_to_seqs: Iterable[int] = ()` is accepted only by `single_turn` and
  `start_turn`; its default replies to no turn.

All publishing methods accept optional `recipients` and `extensions`. Recipients and
ObjectKeys preserve caller order and duplicates. `extensions` must be a JSON
object. The SDK rejects invalid local inputs before publishing.

The SDK writes caller-supplied `reply_to_seqs` in its original order into the
payload's JSON numeric array of the same name, without sorting or automatic
deduplication. Local validation covers protocol field shape only: each element
must be a positive uint64 integer other than bool, and duplicate seq values are
invalid. Invalid input raises `ChatProtocolError`.
The SDK does not call Fetch or GetStatus for reply references, validate target
existence, Channel, or event kind, or
resolve a seq back to a `TurnRef`. The caller selects valid targets: earlier
`turn.single` or `turn.start` creation events in the same Channel, whose turns
may still be open. [Protocol section 11](CHAT_PROTOCOL.md#11-replies) is the
authority for complete target rules.

### 3.1 Independent Writing Objects

`TurnWriter` is returned by `start_turn()` or `resume_turn()` and is bound to the
client that created it and that client's principal and Channel. It exposes
read-only `turn_id` and `creation_seq` properties. Internally it stores the last
confirmed committed `last_seq` and an `open`, `completed`, or `unresolved` state.
It does not store the full message content or conversation history and cannot
be rebound to another client.

The public write signatures are:

```text
writer.append(*, content, recipients=(), object_keys=(), extensions=None) -> int
writer.complete(*, recipients=(), extensions=None) -> int
```

A newly created object's `last_seq` equals `creation_seq`. Each append or complete
uses its current `last_seq` as `pre_seq`. A successful append updates `last_seq`
before returning; a successful complete updates `last_seq` and enters `completed`
before returning.
State checks, publishing, retries, and result registration are serialized within
one object; objects for different turns can write independently. This is local
write state, not a conversation projection. `completed` means only that this
object's end event committed and does not change the protocol's earliest-terminal rule.

Only one writing object is responsible for a turn at a time. Its caller holds it
for subsequent appends and completion. After observing an effective cancellation
through normal reads, the caller stops writing and releases the object.
`cancel_turn()` only publishes the cancellation fact; it does not look up or
modify objects. Releasing or destroying an object performs no RPC, automatically
publishes neither end nor cancel, and does not mean already-sent requests were
cancelled. The client retains no object references, so no per-turn reclamation
API or terminal-record table is needed. Every object write first checks the bound
client's lifecycle. All bound objects become unusable for writing when their
client fails or closes.

If an append or complete publish has an uncertain outcome, the object becomes
`unresolved`. Subsequent append and complete calls raise `TurnWriterStateError`
and cannot continue from the old tail. Neither reading history once nor releasing
an object proves an old write can no longer commit; resuming the turn requires
the old-write conditions in section 3.2. A failure before Publish is sent, or one
where the entire logical publish is known not to have committed and can no longer
commit, preserves the original `last_seq` and `open` state. A `completed` object
also rejects further writes. A failed `start_turn()` returns no object; if the
creation outcome is uncertain, the original write must likewise be resolved
before deciding whether to resume.

### 3.2 Explicitly Resume An Existing Streaming Turn

This example resumes an unfinished old turn whose prior writes satisfy the
handoff conditions below:

```python
writer = chat.resume_turn("unfinished-1", state_start_seq=1)
seq = writer.append(content=(TextPart("Continuing."),))
seq = writer.complete()
```

The public signature is
`resume_turn(turn_id: str, *, state_start_seq: int = 1) -> TurnWriter`. It recovers
only the target owned by the current client principal in the bound Channel and
publishes no event. `state_start_seq` must be a positive uint64 integer other
than bool and must not be later than the target creation event.

Before recovery, the caller must stop the old object's writes and ensure all
old requests for that turn have ended and no late commit remains possible.
An uncertain publish must first have its outcome resolved or be guaranteed
unable to commit. Discarding an unresolved object and calling resume cannot
bypass this precondition. The client neither registers objects by turn nor
coordinates the handoff for callers.

The SDK obtains one GetStatus watermark and pages from `state_start_seq` through
that fixed range, using temporary state to recover only the target's creation
seq, tail, and terminal state. If the target remains writable, it returns a new
`open` object with `creation_seq` and `last_seq` set. A missing target raises
`TurnNotFoundError`; an ended target raises `TurnWriterStateError`. Failure
returns no object and leaves no target state in the client. Recovery parses
`submission.reserve` and advances its read position without treating it as a
turn event.

Only explicit `resume_turn()` scans history for recovery. Construction, ordinary
`fetch_page()`, creation, cancellation, and object append/complete calls never
trigger recovery automatically. Recovery errors and the scope of client failure
follow sections 4 and 5.

### 3.3 Publish A Submission Reservation Control Event

```python
reserve_seq = chat.reserve_submissions(
    reserved_through=10000,
    recipients=(),
    extensions=None,
)
```

The public signature is
`reserve_submissions(reserved_through: int, *, recipients=(), extensions=None) -> int`.
It publishes one `submission.reserve` control event and returns that reservation
event's committed OpenEvent seq. The result is not a `submission_id` and does
not confirm that any user message was committed. `reserved_through` must be a
positive uint64 integer other than bool; invalid local input raises
`ChatProtocolError`. The [public protocol](CHAT_PROTOCOL.md) defines the complete
payload field rules. This method accepts no turn, content, reply, or attachment
arguments.

This method does not call Fetch or GetStatus or create a writing object.
It uses the same UUID rules in section 4 for its logical publish and
retries. The SDK does not calculate batch sizes, compare historical reservation
limits, allocate `submission_id` values, or arbitrate between allocators.
The calling application manages allocation ranges, in-memory inventory, expiry,
and batch transitions; these do not change the generic SDK concurrency contract.

## 4. UUIDs, Retries, And Errors

Each logical publish obtains exactly one UUID internally from `get_uuid()`. The
SDK freezes the payload, recipients, ObjectKeys, and UUID before the first
`PublishAutoSeq` attempt. A retry reuses all of them unchanged.

`DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`, and `INTERNAL` are retried up
to `max_retries` additional times. Every retry waits a fixed 100 ms to avoid
immediate repeated requests during a transient server or network fault. The SDK
does not use exponential backoff or random jitter.

If a same-UUID publish retry returns `ALREADY_EXISTS`, the SDK calls
`get_seq_by_uuid(uuid)` and returns the original committed seq. It never scans
history for UUIDs and never allocates a second UUID for that logical publish.

Section 3.1 covers unresolved writing objects. GetStatus and Fetch errors during
explicit recovery follow section 5. A `PublishFailedError` can still mean that a
non-reconciled publish committed.
Its `uuid` attribute is always the non-zero UUID obtained for that logical
publish. Callers may include it with its structured failure information in a
redacted error log; it is not an argument for another Chat write and does not
provide cross-process recovery or resubmission. Applications must choose their
business recovery policy.

Errors that have a root cause expose the same read-only `failure: FailureInfo`.
`FailureInfo` is publicly exported from `openevent.chat_sdk`:

```python
@dataclass(frozen=True)
class FailureInfo:
    stage: str
    category: Literal[
        "external_unavailable", "authentication", "permission", "not_found",
        "protocol", "contract", "lifecycle",
    ]
    grpc_code: grpc.StatusCode | None
    retryable: bool
    detail: str
```

`stage` identifies the operation, such as `GetChannel`, `GetStatus`, `Fetch`,
`get_uuid`, `PublishAutoSeq`, or `GetSeqByUuid`. `category` is the stable value
for application branching; callers must not parse `detail`. `grpc_code` is the
last received gRPC status, or `None` when no status exists. `retryable` means
that rebuilding a client may succeed after an external condition is restored;
it is not an instruction to retry immediately and never makes an uncertain
publish safe to resend. `detail` must state the concrete reason, for example
`Fetch failed after 4 attempts` or `Fetch returned seq 119 followed by seq 118`,
and must not contain tokens, ObjectKeys, payloads, or full text.

Categories follow the root cause. A legal call that cannot complete because an
external service or transport is temporarily unavailable is
`external_unavailable`. Temporary capacity or quota exhaustion is also
`external_unavailable`. Invalid credentials, an ACL denial, and a resource that
definitively does not exist are `authentication`, `permission`, and `not_found`.
Invalid `chat.v1` input or history is `protocol`. An OpenEvent response that
violates its public contract, confirmed persistent-data damage, a locally
validated SDK request that is still rejected as invalid, or an impossible local
SDK state is `contract`.
`lifecycle` is used only when an already-sent operation loses its result because
of explicit close and no more specific root cause exists.

gRPC statuses and local causes have the following stable mapping:

| gRPC status or local cause | `category` |
| --- | --- |
| `CANCELLED`, `DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`, temporary `RESOURCE_EXHAUSTED`, and transient `INTERNAL` after retries are exhausted | `external_unavailable` |
| `UNAUTHENTICATED` | `authentication` |
| `PERMISSION_DENIED` | `permission` |
| A resource that definitively does not exist | `not_found` |
| Invalid `chat.v1` input or history | `protocol` |
| `DATA_LOSS`, persistent-data damage confirmed by the SDK, `INVALID_ARGUMENT` for an SDK-validated request, unexpected `ABORTED`/`ALREADY_EXISTS`, an invalid response, or impossible local state | `contract` |
| Termination caused only by explicit close | `lifecycle` |

When a status has a more specific meaning in the public contract of its RPC,
that meaning wins; callers and the SDK must not infer a category from error text.
The exact status always remains in `grpc_code`. `retryable=true` is used only
for `external_unavailable` when rebuilding the client may succeed after the
external condition is restored. It is false for every other category.

`ChannelInitializationError`, `FetchPageError`, `SyncReadError`, `UuidAllocationError`,
`PublishFailedError`, and `ClientFailedError` expose
`failure`. Wrapper errors preserve the original `FailureInfo`; they must not
collapse it into a generic client-failed message. `PublishFailedError` also
exposes `uuid`. Normal `chat.close()` and local turn precondition errors use
their specific return, exception, and message instead of fabricating a remote
failure.

The following errors are part of the public API:

- `ChatProtocolError`: invalid SDK input or one payload.
- `ChannelInitializationError`: a client could not become ready; `failure`
  identifies Channel validation and the root cause.
- `FetchPageError`: a one-page Fetch or message parse failed; `failure`
  identifies the operation and root cause.
- `TurnNotFoundError`: explicit recovery did not find the target turn.
- `TurnWriterStateError`: the writing object is completed or unresolved, or
  the target of explicit recovery has ended.
- `SyncReadError` and `UuidAllocationError`: no publish was sent; `failure`
  identifies the failed operation or lifecycle root cause.
- `PublishFailedError`: PublishAutoSeq or UUID lookup failed after a UUID was
  obtained, or client lifecycle interrupted a PublishAutoSeq attempt already
  started. Its `uuid` and `failure` are available for logging. Regardless of
  `failure.retryable`, callers must not automatically resend a new Chat message.
- `ClientFailedError` and `ClientClosedError`: the client has permanently failed
  or is closed.

## 5. Client Lifecycle

A client is ready after construction, then becomes either permanently failed or
closed. A failed or closed client cannot be recovered or reused. `resume_turn()`
is one complete recovery operation: exhausted retries or a non-retryable remote
error from either GetStatus or recovery Fetch permanently fail the entire client.
Invalid arguments and missing or ended targets reject only the current recovery.
Invalid protocol history, an invalid Fetch response, or a local derived-state
failure also permanently fail the client. The exception raised by the current
synchronous method reports the error; later client calls and writes through bound
objects raise `ClientFailedError` preserving the original `FailureInfo`.
A normal one-page Fetch error only ends that call; the caller
decides whether to retry or exit.

```python
chat.close()
```

`close()` is idempotent and supports context-manager use. It rejects new work,
wakes retry waits, and waits for work already in flight, including writes through
bound objects. After close, writes through bound objects raise `ClientClosedError`.
Close does not enumerate writing objects, cancel an RPC already sent, or close
the injected OpenEvent client.

The underlying connection is managed by its owner. To interrupt in-flight RPCs
on that connection, the owner calls `OpenEventClient.close()` under the OpenEvent
SDK's public close contract. Closing a shared connection affects every Chat client
using it; cancelling an RPC does not prove that a remote write was never committed.

## 6. Deployment And Limits

Deploy at most one active Chat writing process for each `(principal, channel_id)`;
that process may hold multiple writing objects for different turns. The
SDK does not provide a cross-process lock, lease, durable cursor, checkpoint,
or application projection. Applications must coordinate writers, save the read
positions and business recovery state they need, and arrange their own reading
and processing after a restart.

SDK construction and object writes do not reconstruct history. The latency of
explicit `resume_turn()` grows with the scanned range; it neither maintains
allocation state nor validates historical reservation amounts.
It accepts at most 1024 ObjectKeys per message and never logs ObjectKey tokens,
tokens, payloads, or full text in SDK errors.
