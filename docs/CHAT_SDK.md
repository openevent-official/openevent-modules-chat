# Chat Python SDK Public Contract

[中文版](CHAT_SDK_cn.md)

> Status: public contract
> Scope: `openevent.chat_sdk` 0.1.0

## 1. Scope and prerequisites

The Chat Python SDK provides `chat.v1` publishing, one-page-at-a-time Fetch reads, and stateless message parsing. It does not provide a worker, Agent runtime, UI, role mapping, application authorization, business idempotency, or a persistent application projection.

The SDK requires Python 3.10 or later, `openevent-sdk>=0.11.1` installed in the current environment, and an existing Channel that the caller can read with `protocol="chat.v1"`. It uses an injected public OpenEvent client; it neither installs the SDK from source nor generates protobuf modules.

`CHAT_PROTOCOL.md` defines payload fields and turn semantics. The OpenEvent API documentation defines authentication, ACL, Fetch, UUID, ObjectKey, and gRPC status semantics. The installed `0.11.1` SDK is the current reference; Python call shapes follow that version's public interfaces.

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

A `ChatProtocolClient` is permanently bound to one nonzero `principal`, one token, and one nonzero `channel_id`. During construction, the SDK verifies that the Channel is readable, its ID matches, and its protocol is `chat.v1`. It returns a ready client without Fetch, history recovery, or background threads. The client keeps no per-turn state table, writer registry, or strong references to writers.

The constructor and `create_client()` accept an optional `channel_validator`, defaulting to `None`. An application can supply a synchronous, read-only validation function. After basic validation, the SDK passes the `ChannelInfo` from the same GetChannel response to it exactly once, without another Channel read. Returning normally means validation passed. An exception causes construction to raise `ChannelInitializationError` with `failure.category="contract"`; validation is not retried, and the original exception text is not included in the failure. Application rules such as membership and naming belong to that function, not to the SDK. Without it, only basic validation runs.

`max_retries` must be a nonnegative integer; `0` means one attempt. The history recovery start is supplied to an explicit `resume_turn()` call, not to construction. The injected client must expose a positive, finite `timeout_ms` in milliseconds and thread-safe `get_channel`, `get_status`, `fetch`, `get_uuid`, `get_seq_by_uuid`, and `publish_auto_seq` methods.

The SDK reads but does not modify the injected client's `timeout_ms`. It does not close that client or its gRPC channel.

`parse_message(event_message)` is a public stateless parser. It strictly parses one OpenEvent `EventMessage` into `ParsedMessage`, validating its `chat.v1` JSON and basic fields. It does not maintain cross-event turn state, perform Fetch, or determine application roles. Applications that page through a bounded history range should reuse this function instead of implementing another payload parser.

`ParsedMessage.payload` also supports the `submission.reserve` control event. `kind` distinguishes event types, and `reserved_through` remains a Python `int`. Field validity follows the [public protocol](CHAT_PROTOCOL.md). This event has no turn identity, so callers must not assume every parsed message has `turn_id`.

The public `ParsedMessage.payload` type branches support `kind="turn.reset"`, preserving the protocol's `turn_id`, `pre_seq`, `content`, and optional `extensions`. Attachments still come from the OpenEvent message's top-level `object_keys`. The parser validates individual fields only; it does not determine whether the target can still be reset or remove earlier parsed messages. Callers maintaining turn projections apply reset according to the protocol.

### 2.1 Reading one page

```python
page = chat.fetch_page(from_seq=8451, limit=100)
for message in page.messages:
    print(message.seq, message.payload)
print(page.next_seq, page.last_seq)
```

`fetch_page()` reads the bound Channel with `only_my_recipient=false`. Each call performs one logical Fetch and does not read the next page automatically. Retries of the same RPC after temporary errors remain part of that logical call. The returned `FetchPage` contains `messages`, `next_seq`, and `last_seq`. The SDK validates Fetch pagination and ordering and parses every message, but does not save the page cursor or modify any writer. Callers continuing the read must use the returned `next_seq` as the next `from_seq`.

`messages` includes parsed `submission.reserve` control events with their original seq values. The SDK does not remove them because they do not create turns.

Applications needing continuous reads loop over `fetch_page()`, save `next_seq` after processing each page, and choose the wait and stopping policy at the tail. The SDK provides no subscription, message callback, scan-progress wait interface, or background polling task, and does not call OpenEvent `Subscribe`.

## 3. Write API

```python
from openevent.chat_sdk import ObjectKey, TextPart, TurnRef

user_creation_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("Hello"),),
    recipients=(),
    object_keys=(ObjectKey(object_id=42, object_token="..."),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(TextPart("Hello"),),
)
reply_creation_seq = writer.creation_seq
seq = writer.append(
    content=(TextPart("The report is ready."),),
    object_keys=(ObjectKey(object_id=43, object_token="..."),),
)
seq = writer.complete()
seq = chat.cancel_turn(target_turn=TurnRef(9002, "other-turn"))
```

A successful return from any publish method means OpenEvent has committed that event. It does not mean an application projection or browser has observed it. `start_turn()` returns the publicly exported `TurnWriter`, whose `creation_seq` identifies the creation event. `single_turn()` returns the creation event seq directly. Both positions can be used in later `reply_to_seqs`. `writer.append()`, `writer.reset()`, `writer.complete()`, `cancel_turn()`, and `reserve_submissions()` return the committed seq of their respective events.

- `single_turn` creates a completed turn; `content` may be empty.
- `start_turn` creates a streaming turn; `content` may be empty, defaulting to `()`. It returns a bound writer only after the `turn.start` commit is confirmed.
- `writer.append` appends content, and `writer.complete` ends the turn normally. They operate only on the bound streaming turn, take no `turn_id`, and do not read history automatically.
- `writer.reset` clears the streaming turn's currently visible body and attachments, replacing them with this call's `content` and `object_keys`; both may be empty. It retains the turn, creation position, and reply relationships and does not end the turn. It cannot undo an effective end/cancel. The public protocol defines the full state rules.
- `cancel_turn` accepts a complete `TurnRef`; the target owner need not be the current client principal.
- `single_turn`, `start_turn`, `writer.append`, and `writer.reset` accept `object_keys: Iterable[ObjectKey] = ()`. Attachments go only into the current event's OpenEvent top-level `object_keys`; they are not inherited from previous events or written back to the creation event. `writer.append` may omit `content` and append only attachments; a call with neither content nor attachments raises `ChatProtocolError`.
- Only `single_turn` and `start_turn` accept `reply_to_seqs: Iterable[int] = ()`, defaulting to no replies.

`start_turn` and `writer.append` both take `content: Iterable[TextPart] = ()`, supporting empty-content starts and attachment-only appends respectively. The SDK validates content and attachment combinations against [protocol section 4](CHAT_PROTOCOL.md#4-content-part). The requirement that each TextPart contain nonempty text remains unchanged; the SDK inserts no placeholder text.

Every publish method accepts optional `recipients` and `extensions`. Recipients and ObjectKeys preserve caller order and duplicates; `extensions` must be a JSON object. The SDK rejects locally detectable invalid input before publishing.

The SDK writes the caller's `reply_to_seqs` into the same-named JSON numeric array in input order, without sorting or automatic deduplication. Local checks cover field shape only: elements must be positive uint64 integers, excluding bool, with no duplicate seq values. Invalid input raises `ChatProtocolError`. The SDK does not call Fetch or GetStatus for reply references, verify target existence, Channel or event kind, or resolve seq values back into `TurnRef`. The caller selects valid targets: earlier `turn.single` or `turn.start` creation events in the same Channel; target turns may still be open. Complete target rules are defined in [protocol section 11](CHAT_PROTOCOL.md#11-reply-relationships).

### 3.1 Independent writers

`TurnWriter` is returned by `start_turn()` or `resume_turn()` and is bound to the creating client, its principal, and its Channel. It exposes read-only `turn_id` and `creation_seq`, and internally retains the last confirmed committed `last_seq` and the state `open`, `completed`, or `unresolved`. It keeps neither the full content nor conversation history and cannot switch clients.

Public write signatures:

```text
writer.append(*, content=(), recipients=(), object_keys=(), extensions=None) -> int
writer.reset(*, content=(), recipients=(), object_keys=(), extensions=None) -> int
writer.complete(*, recipients=(), extensions=None) -> int
```

A new writer's internal `last_seq` points to the turn's confirmed chain tail. Each append, reset, or complete uses the current `last_seq` for `pre_seq`. Successful append and reset calls update `last_seq` before returning. Successful complete updates `last_seq` and enters `completed` before returning. State checks, publishing, retries, and result registration are serialized within each writer; writers for different turns can write independently. Writer state describes local publishing, not the conversation projection: `completed` means only that this writer's end was committed and does not change the protocol's earliest-terminal-event rule.

After a successful reset, the writer remains `open`; the next append/reset/complete continues from the reset seq. `turn_id` and `creation_seq` remain unchanged. `content: Iterable[TextPart] = ()` and `object_keys` are materialized and frozen before publication, using the same writer lock and the UUID retry rules in section 4. The SDK neither stores nor returns replaced content and does not read history to detect remote cancellation. A successful reset return proves only that the reset event was committed. If a valid end/cancel occurred earlier, the projection remains terminal, and callers stop writing based on normal reads.

Only one writer owns a turn at a time. The caller retains it for subsequent appends and completion. After normal reading observes an effective cancellation, the caller stops writing and releases the writer. `cancel_turn()` publishes a cancellation fact without looking up or changing writers. Releasing or destroying a writer performs no RPC, publishes no end/cancel, and does not mean previously sent requests were cancelled. The client retains no writer references, so no per-turn reclamation interface or terminal-record table is needed. Each write first checks the bound client's lifecycle; no bound writer can continue after the client fails or closes.

If the outcome of append, reset, or complete is uncertain, the writer enters `unresolved`. Subsequent append, reset, and complete calls raise `TurnWriterStateError`; they cannot continue from the old tail. One history read or releasing the writer cannot prove an old write will never commit. Recovering that turn requires the old-write conditions in section 3.2. If Publish has not been sent, or the entire logical publish is known not to have committed and cannot commit later, failure preserves the original `last_seq` and `open` state. A `completed` writer also rejects further writes. Failed `start_turn()` calls return no writer; an uncertain creation likewise requires resolving the original write before deciding whether to recover.

Reset is not a remedy for an unknown publish outcome. Before retrying the model, the application must stop old model output from entering this writer and wait for old Chat writes to finish. Old output must not append after reset. Reset cannot make an `unresolved` writer `open`, and a Fetch that does not find the old write does not remove the handoff precondition.

### 3.2 Explicitly recovering an existing streaming turn

This example recovers an unfinished turn whose old writes satisfy the handoff conditions below:

```python
writer = chat.resume_turn("unfinished-1", state_start_seq=1)
seq = writer.append(content=(TextPart("Continuing."),))
seq = writer.complete()
```

The public signature is `resume_turn(turn_id: str, *, state_start_seq: int = 1) -> TurnWriter`. It recovers only the target turn owned by the current client principal in the bound Channel and publishes nothing. `state_start_seq` must be a positive uint64 integer, excluding bool, and cannot be later than the target creation event.

Before recovery, callers must stop the old writer and confirm that all old requests for the turn have finished and cannot commit late. When a publish is uncertain, first confirm its original outcome or ensure that the original request cannot commit again. Dropping an unresolved writer and recovering does not bypass this requirement. The client does not register writers for a turn or coordinate their handoff.

The SDK obtains one GetStatus watermark and pages through the fixed range from `state_start_seq`, using temporary state to recover only the target creation seq, tail, and terminal state. If the target remains writable, it returns a new `open` writer with `creation_seq` and `last_seq` set. Missing targets raise `TurnNotFoundError`; terminal targets raise `TurnWriterStateError`. Failure returns no writer and leaves no target state in the client. Recovery parses `submission.reserve` and advances its read position without treating it as a turn event.

Recovery recognizes effective `turn.reset` events according to the public protocol, advances the tail to their seq, and processes subsequent appends and terminal events. Reset neither changes the creation seq nor makes a terminal target writable. The recovered writer needs no body or attachment contents from before or after reset.

Only explicit `resume_turn()` scans history for recovery. Construction, ordinary `fetch_page()`, creation, cancellation, and writer append/reset/complete do not trigger recovery automatically. Recovery errors and their client-wide effects follow sections 4–5.

### 3.3 Publishing a submission reservation control event

```python
reserve_seq = chat.reserve_submissions(
    reserved_through=10000,
    recipients=(),
    extensions=None,
)
```

The public signature is `reserve_submissions(reserved_through: int, *, recipients=(), extensions=None) -> int`. It publishes a `submission.reserve` control event and returns that event's committed OpenEvent seq. The return value is not a `submission_id` and does not mean a user message has been committed. `reserved_through` must be a positive uint64 integer, excluding bool; invalid local input raises `ChatProtocolError`. The [public protocol](CHAT_PROTOCOL.md) defines all payload fields. The method accepts no turn, body, reply, or attachment arguments.

This method calls neither Fetch nor GetStatus and creates no writer. It uses the same-UUID publish and retry rules in section 4. The SDK neither calculates batch sizes nor compares historical reservation limits, assigns `submission_id`, or coordinates allocators. The calling application manages allocation ranges, in-memory inventory, expiration, and batch changes; the general SDK concurrency contract is unchanged.

## 4. UUIDs, retries, and errors

Each logical publish internally obtains exactly one UUID through `get_uuid()`. Before the first `PublishAutoSeq`, the SDK freezes payload, recipients, ObjectKeys, and UUID. Retries reuse all of them unchanged.

`DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`, and `INTERNAL` permit at most `max_retries` additional attempts. A fixed 100 ms wait precedes each retry to avoid immediate repeated requests during temporary network or server failures. The SDK uses neither exponential backoff nor jitter.

If a Publish retry with the same UUID returns `ALREADY_EXISTS`, the SDK calls `get_seq_by_uuid(uuid)` and returns the previously committed seq. It neither scans historical UUIDs nor obtains a second UUID for the same logical publish.

Section 3.1 defines unresolved writer handling. GetStatus and Fetch failures during explicit recovery follow section 5. `PublishFailedError` can still describe a publish that committed without successful reconciliation. Its `uuid` is always the nonzero UUID obtained for this logical publish and can be recorded with structured failure information in redacted logs. It is not an argument for the next Chat write and provides no cross-process recovery or resend capability. Applications choose their own business recovery policy.

Public errors needing a root cause expose the same read-only `failure: FailureInfo` structure. `FailureInfo` is publicly exported from `openevent.chat_sdk`. Its structure is:

```python
@dataclass(frozen=True)
class FailureInfo:
    stage: str
    category: Literal[
        "external_unavailable", "authentication", "permission", "not_found",
        "request_rejected", "protocol", "contract", "lifecycle",
    ]
    grpc_code: grpc.StatusCode | None
    detail: str
```

- `stage` identifies the failed step, such as `GetChannel`, `GetStatus`, `Fetch`, `get_uuid`, `PublishAutoSeq`, or `GetSeqByUuid`.
- `category` is the stable application branching key; do not parse `detail`.
- `grpc_code` retains the last received gRPC status, or `None` if the failure is not a gRPC result or no status was received.
- `detail` must describe the specific cause, such as `Fetch failed after 4 attempts` or `Fetch returned seq 119 followed by seq 118`, rather than only `operation failed`. It may be used in redacted logs but must not contain tokens, ObjectKeys, payloads, or full message bodies.

Categories follow the root cause. Temporary inability of an external service or transport to complete a valid call is `external_unavailable`. Invalid credentials, ACL denial, and explicitly missing resources are `authentication`, `permission`, and `not_found`. A remote rejection under public parameter, membership, or request-size rules is `request_rejected`. Invalid `chat.v1` input or history is `protocol`. Only an established contract contradiction, a server report of persistent data corruption, or an impossible local state is `contract`. `lifecycle` applies only when an already-issued operation cannot produce a result solely because of explicit close and no more specific cause exists.

Passing local field-shape checks does not establish that all conditions still hold at server commit time: for example, a recipient may be validly removed from the Channel before publication commits, causing `PublishAutoSeq` to return `INVALID_ARGUMENT` under the OpenEvent contract. That response is not evidence of infrastructure corruption. The SDK performs no extra membership or deployment-limit queries to classify errors.

Interpret gRPC status with the operation stage. Stable mappings are:

| gRPC status or local cause | `category` |
| --- | --- |
| `CANCELLED`, `DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`, temporary `RESOURCE_EXHAUSTED`, and temporary `INTERNAL` after retry exhaustion | `external_unavailable` |
| `PublishAutoSeq` returning `INVALID_ARGUMENT` (including recipient membership rejection at commit time) or `RESOURCE_EXHAUSTED` (payload size limit) | `request_rejected` |
| Direct `WriteObject` returning `INVALID_ARGUMENT`, except for the established contract contradiction described below | `request_rejected` |
| Direct `WriteObject` returning `RESOURCE_EXHAUSTED` (object-file space, quota, or inode exhaustion) | `external_unavailable` |
| `UNAUTHENTICATED` | `authentication` |
| `PERMISSION_DENIED` | `permission` |
| Explicitly missing resource | `not_found` |
| Invalid `chat.v1` input or history | `protocol` |
| `DATA_LOSS`, SDK-confirmed persistent data corruption, a rejection that demonstrably violates the RPC's public contract, unexpected `ABORTED`/`ALREADY_EXISTS`, invalid response, or impossible local state | `contract` |
| Operation termination caused solely by explicit close | `lifecycle` |

RPC-specific mappings take precedence over generic status rows. Direct-RPC rows are for applications reusing `FailureInfo`; they add no object-writing method to the Chat SDK. For `WriteObject`, if the caller has verified that the actual request satisfies every relevant public static precondition—including principal, metadata, and data constraints—but still receives `INVALID_ARGUMENT`, the contradiction is established and the category is `contract`. Field-shape checks alone do not establish this. A `PublishAutoSeq` recipient's membership can change before commit, so local validation cannot prove that it remains valid; this static-precondition reasoning does not apply.

Classification uses only the RPC stage, public semantics, and established facts, without parsing error text. Preserve the concrete status in `grpc_code`. A `PublishAutoSeq` size limit is not a temporary resource fault that disappears by waiting. Callers handle errors using `category` and the operation stage; classification alone does not mean a write can safely be resent.

A final remote rejection does not prove that earlier uncertain attempts with the same UUID did not commit. Commit determination for the complete logical publish, unresolved writer state, and resend restrictions continue to follow this section and section 3.1.

`ChannelInitializationError`, `FetchPageError`, `SyncReadError`, `UuidAllocationError`, `PublishFailedError`, and `ClientFailedError` all expose `failure`. Wrappers must preserve the original `FailureInfo`, rather than reducing it to a generic “client failed” in `ClientFailedError`. `PublishFailedError` also exposes `uuid`. Normal `chat.close()` and local turn precondition errors use their specific returns, exception types, and messages without fabricating remote failure information.

Public API errors:

- `ChatProtocolError`: invalid SDK input or individual payload.
- `ChannelInitializationError`: the client could not become ready; `failure` provides the Channel validation stage and cause.
- `FetchPageError`: one-page Fetch or message parsing failed; `failure` provides the actual stage and cause.
- `TurnNotFoundError`: explicit recovery did not find the target turn.
- `TurnWriterStateError`: the writer is completed or unresolved, or the explicit recovery target is terminal.
- `SyncReadError` and `UuidAllocationError`: Publish has not been sent; `failure` identifies the failed RPC or lifecycle cause.
- `PublishFailedError`: after obtaining the UUID, PublishAutoSeq or UUID lookup failed, or an initiated Publish was interrupted by the client lifecycle. Its `uuid` and `failure` can be logged. Regardless of the error category, this does not authorize automatically sending a new Chat message.
- `ClientFailedError` and `ClientClosedError`: the client has permanently failed or closed.

## 5. Client lifecycle

After construction the client is ready and can only permanently fail or close. Failed or closed clients cannot recover or be reused. `resume_turn()` is a complete recovery operation: exhausted retries or nonretryable remote errors from GetStatus or recovery Fetch permanently fail the entire client. Invalid arguments, a missing target, or a terminal target reject only that recovery call. Invalid protocol history, an invalid Fetch response, or corrupted derived local state also permanently fail the client. Errors are reported through the current synchronous method's exception. Later client calls and bound writer writes raise `ClientFailedError` preserving the original `FailureInfo`. Ordinary one-page Fetch call errors end only that call; the caller decides whether to retry or exit.

```python
chat.close()
```

`close()` is idempotent and supports a context manager. It rejects new work, wakes retry waits, and waits for in-flight work, including bound writer calls, to finish. Bound writer writes after close raise `ClientClosedError`. Closing does not enumerate writers, cancel issued RPCs, or close the injected OpenEvent client.

The underlying connection is managed by its owner. To interrupt its in-flight RPCs, the owner calls `OpenEventClient.close()` according to the OpenEvent SDK's public close contract. Closing a shared connection affects all Chat clients using it. Cancelling an RPC does not prove that a remote write did not commit.

## 6. Deployment and limitations

Deploy at most one active Chat writing process per `(principal, channel_id)`. One process may hold writers for different turns. The SDK provides no cross-process lock, lease, persistent cursor, checkpoint, or application projection. Applications coordinate writers, persist the read positions and business recovery state they need, and arrange reads and processing after restart.

SDK construction and writer operations do not reconstruct history. Explicit `resume_turn()` cost grows with the scanned range; it neither maintains allocation state nor validates historical reservation capacity. Each message accepts at most 1024 ObjectKeys. SDK errors never log ObjectKey tokens, principal tokens, payloads, or full bodies.

The reset, empty-content start, and attachment-only append extensions still use `chat.v1`. Before publishing these events, all readers in the deployment—including SDKs, Agents, Chat Server, and browsers—must support the corresponding parsing, content rules, and projection behavior. Older readers may not recognize reset or may reject empty-content events allowed by the new rules, so they are not compatible. Reset changes only the currently visible body and attachment collection; it deletes neither historical ObjectKeys nor ObjectStorage objects.
