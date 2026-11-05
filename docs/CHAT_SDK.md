# Chat Python SDK Public Contract

[中文版](CHAT_SDK_cn.md)

> Status: public contract
> Scope: `openevent.chat_sdk` 0.1.0

## 1. Scope and prerequisites

The Chat Python SDK provides `chat.v1` publishing, one-page-at-a-time Fetch reads, content reads by terminal position, and stateless message parsing. It does not provide a worker, Agent runtime, UI, role mapping, application authorization, business idempotency, or a persistent application projection.

The SDK requires Python 3.10 or later, `openevent-sdk>=0.13.0` installed in the current environment, and an existing Channel that the caller can read with `protocol="chat.v1"`. It uses an injected public OpenEvent client; it neither installs the SDK from source nor generates protobuf modules.

`CHAT_PROTOCOL.md` defines payload fields and turn semantics. The OpenEvent API documentation defines authentication, ACL, Fetch, UUID, ObjectKey, and gRPC status semantics. SDK `0.13.0` is the current reference; Python call shapes follow that version's public interfaces.
OpenEvent's contract guarantees the fields, ordering, and cursors in its successful responses. The Chat SDK validates its own inputs, Chat messages, and content-reconstruction relationships without revalidating those underlying results.

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

A `ChatProtocolClient` is permanently bound to one nonzero `principal`, one token, and one nonzero `channel_id`. During construction, the SDK reads the bound Channel and verifies that it is readable with protocol `chat.v1`. It returns a ready client without Fetch, history recovery, or background threads. The client keeps no per-turn state table, writer registry, or strong references to writers.

The constructor and `create_client()` accept an optional `channel_validator`, defaulting to `None`. An application can supply a synchronous, read-only validation function. After basic validation, the SDK passes the `ChannelInfo` from the same GetChannel response to it exactly once, without another Channel read. Returning normally means validation passed. An exception causes construction to raise `ChannelInitializationError` with `failure.category="contract"`; validation is not retried, and the original exception text is not included in the failure. Application rules such as membership and naming belong to that function, not to the SDK. Without it, only basic validation runs.

`max_retries` must be a nonnegative integer; `0` means one attempt. The SDK provides no scanning recovery entry point for unfinished turns; applications reconstruct state and directly construct a writer. The injected client must expose a positive, finite `timeout_ms` in milliseconds and thread-safe `get_channel`, `fetch`, `get_uuid`, `get_seq_by_uuid`, and `publish_auto_seq` methods.

The SDK reads but does not modify the injected client's `timeout_ms`. It does not close that client or its gRPC channel.

## 3. Reading and parsing

### 3.1 Stateless message parsing

`parse_message(event_message)` is a public stateless parser. It strictly parses one nonsystem Chat `EventMessage` into `ParsedMessage`, validating its `chat.v1` JSON and basic fields. It does not maintain cross-event turn state, perform Fetch, or determine application roles. Applications reading OpenEvent history directly must first exclude system messages according to the [OpenEvent system protocol](../openevent-sdk/docs/SYSTEM_PROTOCOL.md), then reuse this function. Passing a system message still raises `ChatProtocolError`.

`ParsedMessage.payload` also supports the `submission.reserve` control event. `kind` distinguishes event types, and `reserved_through` remains a Python `int`. Field validity follows the [public protocol](CHAT_PROTOCOL.md). This event has no turn identity, so callers must not assume every parsed message has `turn_id`.

The public `ParsedMessage.payload` type branches support `kind="turn.reset"`, preserving the protocol's `turn_id`, `pre_seq`, `content`, and optional `extensions`. Attachments still come from the OpenEvent message's top-level `object_keys`. The parser validates individual fields only; it does not determine whether the target can still be reset or remove earlier parsed messages. Callers maintaining turn projections apply reset according to the protocol.

### 3.2 Reading one page

The public signature is `fetch_page(from_seq: int, limit: int = 100) -> FetchPage`; both arguments may be passed positionally or by keyword.

- `from_seq` is required and specifies the scan's starting position. It must be an integer in `0..18446744073709551615`; zero is allowed, but bool is not.
- `limit` defaults to 100 and must be an integer in `1..1000`, excluding bool.

For a usable client, invalid arguments raise `ChatProtocolError` without issuing Fetch or permanently failing the client; failed or closed clients still follow the lifecycle rules in section 7.

```python
page = chat.fetch_page(from_seq=8451, limit=100)
for message in page.messages:
    print(message.seq, message.payload)
print(page.next_seq, page.last_seq)
```

`fetch_page()` reads the bound Channel with `only_my_recipient=false`. Each call performs one logical Fetch and does not read the next page automatically. Retries of the same RPC after temporary errors remain part of that logical call. The returned `FetchPage` contains `messages`, `next_seq`, and `last_seq`. The SDK skips OpenEvent system messages, parsing and returning only the remaining Chat messages. System payloads are not validated as `chat.v1`; nonsystem messages still undergo strict parsing. `next_seq` and `last_seq` retain their original response values and are not recalculated from filtered messages. A system-only page returns empty `messages`; callers must still continue from `next_seq` and must not treat an empty page as the tail. The SDK does not save the page cursor or modify any writer.

`messages` includes parsed `submission.reserve` control events with their original seq values. The SDK does not remove them because they do not create turns.

Applications needing continuous reads loop over `fetch_page()`, save `next_seq` after processing each page, and choose the wait and stopping policy at the tail. The SDK provides no subscription, message callback, scan-progress wait interface, or background polling task, and does not call OpenEvent `Subscribe`.

### 3.3 Read content by terminal position

The public signature is `get_turn_content(terminal_seq: int) -> TurnContent`, accepting a positional or keyword argument.
`terminal_seq` must be a positive uint64 integer, excluding bool; invalid local shape raises `ChatProtocolError`.
The caller supplies a confirmed effective single, end, or cancel terminal position in the bound Channel. Cancelled turns remain readable even though they cannot be reply targets.

Results follow the protocol's final-content rules: single returns that event's content; a normal end returns the effective content identified by `content_seqs`;
an error end returns empty body and attachments; cancel returns the content effective at cancellation, applies reset replacement rules, and includes the effective cancel's own attachments.

A call may require multiple Fetch requests. Normal-end read cost depends on the positions of its effective-content events; cancellation reads may also include other turns' messages between creation and termination.
Temporary memory does not accumulate with the total raw events and unreturned `extensions` from processed pages. The final returned content and necessary read-validation state still occupy memory.

The publicly exported immutable result types are:

```text
TurnAttachment(object_key: ObjectKey, event_seq: int)
TurnContent(
    turn_ref: TurnRef,
    start_seq: int,
    terminal_seq: int,
    state: "completed" | "cancelled",
    content: tuple[TextPart, ...],
    attachments: tuple[TurnAttachment, ...],
    reply_to_seqs: tuple[int, ...],
    error: str | None = None,
)
```

`content` and `attachments` preserve effective-event order and each event's original order. Attachments retain their source event seq; no object file or metadata is read.
`reply_to_seqs` always comes from the original creation event. `error` is `None` normally and `"turn_too_long"` for an error end.
The method returns only after the complete read succeeds, without partial results, turn caching, writer recovery, or a persistent history projection.
It validates identities and manifest links among the events read, but does not scan omitted gaps to prove there was no earlier cancel. It does not replace the caller's effective-terminal confirmation.

RPC or read-validation failures raise `TurnContentError`, preserving the original `FailureInfo`. Missing targets or referenced events, nonterminal targets, and reconstruction contradictions use
`stage="Fetch", category="contract"`. Invalid messages retain the `protocol` category. Ordinary remote errors end only this read; protocol or contract contradictions fail the client under section 7.
Retries apply only to the current RPC under section 6, without restarting the entire read.

## 4. Data representation and serialization

The SDK parses only Chat base fields into Python values. When `ParsedMessage.payload` contains `extensions`, its value is the publicly exported
`RawExtensions`, not a `dict`; other fields retain their existing types. `RawExtensions(json_text: str)` is an immutable wrapper for JSON object source text,
available through its read-only `json_text` attribute. Construction checks valid JSON syntax and an outermost object, raising `ChatProtocolError` for invalid input.
Extension values are not converted into numbers or nested Python objects and do not participate in Chat field, chain-tail, or terminal-state decisions.
Valid JSON numbers such as `1e400` are therefore not rejected because of floating-point overflow.

Parsing preserves the extension object's source text, including number notation, whitespace, and escapes; it does not promise to preserve the entire payload's
source text. Applications that interpret extensions choose a JSON parsing method suitable for their data and apply it to `json_text`. Callers previously reading
`payload["extensions"]` as a dictionary must adapt. Callers forwarding extensions use `RawExtensions` directly without decoding it.

The public function `serialize_payload(payload: dict) -> bytes` encodes a payload as UTF-8 JSON, embedding a top-level `extensions` value of type
`RawExtensions` as the original JSON object, not as a string. It only serializes; it does not validate Chat protocol fields, event validity, or turn state,
so it also supports payloads whose fields have already been converted by Chat Server. `parse_message` and publishing methods retain their respective protocol
validation responsibilities. Do not apply ordinary `json.dumps` directly to a payload containing `RawExtensions`, or decode and re-encode extensions to forward
their original text.

## 5. Write API

```python
from openevent.chat_sdk import ObjectKey, TextPart, TurnRef

user_start_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("Hello"),),
    recipients=(),
    object_keys=(ObjectKey(object_id=42, object_token="..."),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_start_seq,),
    content=(TextPart("Hello"),),
)
seq = writer.append(
    content=(TextPart("The report is ready."),),
    object_keys=(ObjectKey(object_id=43, object_token="..."),),
)
seq = writer.complete()
seq = chat.cancel_turn(target_turn=TurnRef(9002, "other-turn"), start_seq=123400)
```

A successful return from any publish method means OpenEvent has committed that event. It does not mean an application projection or browser has observed it. `start_turn()` returns the publicly exported `TurnWriter`, whose `start_seq` identifies the creation event. `single_turn()` returns the creation event seq directly. The single return position can be used directly as a reply target; a streaming turn requires the effective terminal position returned by `writer.complete()`, not `writer.start_seq`. `writer.append()`, `writer.reset()`, `writer.complete()`, `cancel_turn()`, and `reserve_submissions()` return the committed seq of their respective events.

- `single_turn` creates a completed turn; `content` may be empty.
- `start_turn` creates a streaming turn; `content` may be empty, defaulting to `()`. It returns a bound writer only after the `turn.start` commit is confirmed.
- `writer.append` appends content, and `writer.complete` ends the turn normally. They operate only on the bound streaming turn, take no `turn_id`, and do not read history automatically.
- `writer.reset` clears the streaming turn's currently visible body and attachments, replacing them with this call's `content` and `object_keys`; both may be empty. It retains the turn, creation position, and reply relationships and does not end the turn. It cannot undo an effective end/cancel. The public protocol defines the full state rules.
- `cancel_turn(*, target_turn: TurnRef, start_seq: int, recipients=(), extensions=None) -> int` accepts the full identity and required creation position. The target owner need not be the current client principal. The SDK validates local shape without extra history reads.
- `single_turn`, `start_turn`, `writer.append`, and `writer.reset` accept `object_keys: Iterable[ObjectKey] = ()`. Attachments go only into the current event's OpenEvent top-level `object_keys`; they are not inherited from previous events or written back to the creation event. `writer.append` may omit `content` and append only attachments; a call with neither content nor attachments raises `ChatProtocolError`.
- Only `single_turn` and `start_turn` accept `reply_to_seqs: Iterable[int] = ()`, defaulting to no replies.

`start_turn` and `writer.append` both take `content: Iterable[TextPart] = ()`, supporting empty-content starts and attachment-only appends respectively. The SDK validates content and attachment combinations against [protocol section 4](CHAT_PROTOCOL.md#4-content-part). The requirement that each TextPart contain nonempty text remains unchanged; the SDK inserts no placeholder text.

Every publish method accepts optional `recipients` and `extensions`. Recipients and ObjectKeys preserve caller order and duplicates. `extensions` accepts a `dict`
representing an ordinary JSON object or the `RawExtensions` defined in section 4; `None` omits the field. Ordinary dictionaries use Python JSON encoding and
cannot contain non-JSON values such as NaN or Infinity. Use `RawExtensions` to preserve original number notation or forward extensions already read.
The SDK rejects locally detectable invalid input before publishing; parameter freezing and retries follow section 6.

The SDK writes the caller's `reply_to_seqs` into the same-named JSON numeric array in input order, without sorting or automatic deduplication. Local checks cover field shape only: elements must be positive uint64 integers, excluding bool, with no duplicate seq values. Invalid input raises `ChatProtocolError`. The SDK does not call Fetch or GetStatus for reply references, verify target existence, Channel, event kind, or terminal state, or resolve seq values back into `TurnRef`. The caller selects valid completed terminal positions in the same Channel; the SDK maintains no target-state table. Complete target rules are defined in [protocol section 12](CHAT_PROTOCOL.md#12-reply-relationships).

### 5.1 Independent writers

`TurnWriter` can be returned by `start_turn()`, or constructed directly from state the caller has already confirmed. It is bound to the supplied client, its principal, and its Channel. It exposes read-only `turn_id` and `start_seq`, and internally retains the last confirmed committed `last_seq`, the current effective-content list `content_seqs`, and the state `open`, `completed`, or `unresolved`. It keeps neither the full content nor conversation history and cannot switch clients.

Public constructor and write signatures:

```text
TurnWriter(client: ChatProtocolClient, turn_id: str, start_seq: int, last_seq: int, *, content_seqs: Iterable[int])
writer.append(*, content=(), recipients=(), object_keys=(), extensions=None) -> int
writer.reset(*, content=(), recipients=(), object_keys=(), extensions=None) -> int
writer.complete(*, recipients=(), extensions=None) -> int
```

`content_seqs` is a required keyword argument; other constructor arguments can be positional or keyword arguments. `client` must be a `ChatProtocolClient`, and `turn_id` follows the existing public protocol rules. `start_seq` and `last_seq` must be positive uint64 integers, excluding bool, with `last_seq >= start_seq`. Invalid arguments raise `ChatProtocolError` without failing the client.

The caller must supply the actual state of a turn owned by the current client principal in the bound Channel that has started and has no effective end/cancel. `start_seq` is its `turn.start` seq; `last_seq` is its confirmed current chain tail, the seq of a valid start, append, or reset. They are equal for a turn containing only its start. `content_seqs` must contain the current effective-content positions defined in protocol section 9. Local checks require a nonempty, strictly increasing list of positive uint64 integers excluding bool, all between start_seq and last_seq, with last_seq as the final entry. The caller supplies an accurate, complete list. The old-write handoff conditions are in section 5.2. The SDK reads no history to verify this state and does not check whether the remote turn exists or is terminal.

```python
from openevent.chat_sdk import TurnWriter

# The application confirmed this unfinished turn's creation seq 210 and current tail 217, and completed the old-write handoff.
writer = TurnWriter(chat, turn_id="unfinished-1", start_seq=210, last_seq=217,
                    content_seqs=(210, 214, 217))
seq = writer.append(content=(TextPart("Continuing."),))
```

Construction is entirely local and only creates an `open` writer: it makes no RPC, allocates no UUID, publishes no `turn.start` or other message, and reads no history. Apart from the effective-content position list, the caller supplies no existing body, attachments, or reply relationships; later writes continue from the original turn's tail. Construction does not check the client lifecycle or make a failed or closed client usable again. Every write still checks the lifecycle under section 7.

A new writer's internal `last_seq` points to the turn's confirmed chain tail. Each append, reset, or complete uses the current `last_seq` for `pre_seq`. Successful append and reset calls update `last_seq` before returning. Successful complete updates `last_seq` and enters `completed` before returning. State checks, publishing, retries, and result registration are serialized within each writer; writers for different turns can write independently. Writer state describes local publishing, not the conversation projection: `completed` means only that this writer's end was committed and does not change the protocol's earliest-terminal-event rule.
After a normal or error end is confirmed committed, the writer releases its in-memory effective-content list; definite failures and unresolved outcomes retain it. The committed normal end's `content_seqs` remains unchanged.

After a successful start the list is `[start_seq]`; a confirmed append adds its seq, and a confirmed reset replaces the whole list with `[reset_seq]`. Empty starts/resets retain their positions. Normal complete generates end with this list and the original start_seq, without body or attachments.

After a successful reset, the writer remains `open`; the next append/reset/complete continues from the reset seq. `turn_id` and `start_seq` remain unchanged. `content: Iterable[TextPart] = ()` and `object_keys` are materialized and frozen before publication, using the same writer lock and the UUID retry rules in section 6. The SDK neither stores nor returns replaced content and does not read history to detect remote cancellation. A successful reset return proves only that the reset event was committed. If a valid end/cancel occurred earlier, the projection remains terminal, and callers stop writing based on normal reads.

Only one writer owns a turn at a time. The caller retains it for subsequent appends and completion. After normal reading observes an effective cancellation, the caller stops writing and releases the writer. `cancel_turn()` publishes a cancellation fact without looking up or changing writers. Releasing or destroying a writer performs no RPC, publishes no end/cancel, and does not mean previously sent requests were cancelled. The client retains no writer references, so no per-turn reclamation interface or terminal-record table is needed. Each write first checks the bound client's lifecycle; no bound writer can continue after the client fails or closes.

If the outcome of append, reset, or complete is uncertain, the writer enters `unresolved`. Subsequent append, reset, and complete calls raise `TurnWriterStateError`; they cannot continue from the old tail. One history read or releasing the writer cannot prove an old write will never commit. Recovering that turn requires the old-write conditions in section 5.2. If Publish has not been sent, or the entire logical publish is known not to have committed and cannot commit later, failure preserves the original `last_seq` and `open` state. A `completed` writer also rejects further writes. Failed `start_turn()` calls return no writer; an uncertain creation likewise requires resolving the original write before deciding whether to recover.

Reset is not a remedy for an unknown publish outcome. Before retrying the model, the application must stop old model output from entering this writer and wait for old Chat writes to finish. Old output must not append after reset. Reset cannot make an `unresolved` writer `open`, and a Fetch that does not find the old write does not remove the handoff precondition.

If normal complete's Publish explicitly returns `RESOURCE_EXHAUSTED` and the complete logical publish has `PublishFailedError.uncertain=False`, the SDK publishes one small error end under the same writer lock and with the original `pre_seq`.
This separate event gets a new UUID, retains recipients, omits the content list and caller extensions, and carries `error="turn_too_long"`.
Only after the error end commits does complete return its seq and enter completed. Failure of the error end follows the same definite-failure/unresolved rules without recursive conversion.
An uncertain normal end can never be converted. This exception does not apply to append/reset or other error codes.

### 5.2 Old-write handoff

To continue an existing turn, the caller must first stop the old writer and confirm that all old requests for that turn have finished and cannot commit late. If a publish is uncertain, first establish its original outcome or ensure that the original request can no longer commit. Then construct `TurnWriter` directly from the current state reconstructed and confirmed by the application, as described in section 5.1. The SDK provides no scanning recovery entry point for unfinished turns; section 3.3 reads terminal content only.

Discarding an unresolved writer and constructing another one cannot bypass this precondition. Two writers cannot concurrently continue the same turn. The client keeps no per-turn writer registry and does not coordinate handoff for the caller.

### 5.3 Publishing a submission reservation control event

```python
reserve_seq = chat.reserve_submissions(
    reserved_through=10000,
    recipients=(),
    extensions=None,
)
```

The public signature is `reserve_submissions(reserved_through: int, *, recipients=(), extensions=None) -> int`. It publishes a `submission.reserve` control event and returns that event's committed OpenEvent seq. The return value is not a `submission_id` and does not mean a user message has been committed. `reserved_through` must be a positive uint64 integer, excluding bool; invalid local input raises `ChatProtocolError`. The [public protocol](CHAT_PROTOCOL.md) defines all payload fields. The method accepts no turn, body, reply, or attachment arguments.

This method calls neither Fetch nor GetStatus and creates no writer. It uses the same-UUID publish and retry rules in section 6. The SDK neither calculates batch sizes nor compares historical reservation limits, assigns `submission_id`, or coordinates allocators. The calling application manages allocation ranges, in-memory inventory, expiration, and batch changes; the general SDK concurrency contract is unchanged.

## 6. UUIDs, retries, and errors

Each logical publish internally obtains exactly one UUID through `get_uuid()`. Before the first `PublishAutoSeq`, the SDK freezes payload, recipients, ObjectKeys, and UUID. Retries reuse all of them unchanged.

`DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`, and `INTERNAL` permit at most `max_retries` additional attempts. A fixed 100 ms wait precedes each retry to avoid immediate repeated requests during temporary network or server failures. The SDK uses neither exponential backoff nor jitter.

If a Publish retry with the same UUID returns `ALREADY_EXISTS`, the SDK calls `get_seq_by_uuid(uuid)` and returns the previously committed seq. It neither scans historical UUIDs nor obtains a second UUID for the same logical publish.

Section 5.1 defines unresolved writer handling. One-page Fetch failures follow section 7. `PublishFailedError` can still describe a publish that committed without successful reconciliation. Its `uuid` is always the nonzero UUID obtained for this logical publish and can be recorded with structured failure information in redacted logs. It is not an argument for the next Chat write and provides no cross-process recovery or resend capability. Applications choose their own business recovery policy.

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

- `stage` identifies the failed step, such as `GetChannel`, `Fetch`, `get_uuid`, `PublishAutoSeq`, or `GetSeqByUuid`.
- `category` is the stable application branching key; do not parse `detail`.
- `grpc_code` retains the last received gRPC status, or `None` if the failure is not a gRPC result or no status was received.
- `detail` must describe the specific cause, such as `Fetch failed after 4 attempts` or `Fetch returned invalid chat.v1 data at seq 119`, rather than only `operation failed`. It may be used in redacted logs but must not contain tokens, ObjectKeys, payloads, or full message bodies.

Categories follow the root cause. Temporary inability of an external service or transport to complete a valid call is `external_unavailable`. Invalid credentials, ACL denial, and explicitly missing resources are `authentication`, `permission`, and `not_found`. A remote rejection under public parameter, membership, or request-size rules is `request_rejected`. Invalid `chat.v1` input or history is `protocol`. Only an established contract contradiction, a server report of persistent data corruption, or an impossible local state is `contract`. `lifecycle` applies only when an already-issued operation cannot produce a result solely because of explicit close and no more specific cause exists.

Passing local field-shape checks does not establish that all conditions still hold at server commit time: for example, a recipient may be validly removed from the Channel before publication commits, causing `PublishAutoSeq` to return `INVALID_ARGUMENT` under the OpenEvent contract. That response is not evidence of infrastructure corruption. The SDK performs no extra membership or deployment-limit queries to classify errors.

Interpret gRPC status with the operation stage. Stable mappings are:

| gRPC status or local cause | `category` |
| --- | --- |
| `CANCELLED`, `DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`, temporary `RESOURCE_EXHAUSTED`, and temporary `INTERNAL` after retry exhaustion | `external_unavailable` |
| `PublishAutoSeq` returning `INVALID_ARGUMENT` (including recipient membership rejection at commit time) or `RESOURCE_EXHAUSTED` (payload size limit) | `request_rejected` |
| Direct `WriteObject` returning `INVALID_ARGUMENT` | `request_rejected` |
| Direct `WriteObject` returning `RESOURCE_EXHAUSTED` (object-file space, quota, or inode exhaustion) | `external_unavailable` |
| `UNAUTHENTICATED` | `authentication` |
| `PERMISSION_DENIED` | `permission` |
| Explicitly missing resource | `not_found` |
| Invalid `chat.v1` input or history | `protocol` |
| `DATA_LOSS`, SDK-confirmed persistent data corruption, a rejection that demonstrably violates the RPC's public contract, unexpected `ABORTED`/`ALREADY_EXISTS`, Chat reconstruction contradiction, or impossible local state | `contract` |
| Operation termination caused solely by explicit close | `lifecycle` |

RPC-specific mappings take precedence over generic status rows. Direct-RPC rows are for applications reusing `FailureInfo`; they add no object-writing method to the Chat SDK.

Classification uses only the RPC stage, public semantics, and established facts, without parsing error text. Preserve the concrete status in `grpc_code`. A `PublishAutoSeq` size limit is not a temporary resource fault that disappears by waiting. Callers handle errors using `category` and the operation stage; classification alone does not mean a write can safely be resent.

A final remote rejection does not prove that earlier uncertain attempts with the same UUID did not commit. Commit determination for the complete logical publish, unresolved writer state, and resend restrictions continue to follow this section and section 5.1.

`ChannelInitializationError`, `FetchPageError`, `TurnContentError`, `UuidAllocationError`, `PublishFailedError`, and `ClientFailedError` all expose `failure`. Wrappers must preserve the original `FailureInfo`, rather than reducing it to a generic “client failed” in `ClientFailedError`. `PublishFailedError` also exposes `uuid` and boolean `uncertain`; only false means the complete publish is known not to have committed and cannot commit later. Normal `chat.close()` and local turn precondition errors use their specific returns, exception types, and messages without fabricating remote failure information.

Public API errors:

- `ChatProtocolError`: invalid SDK input or individual payload.
- `ChannelInitializationError`: the client could not become ready; `failure` provides the Channel validation stage and cause.
- `FetchPageError`: one-page Fetch or message parsing failed; `failure` provides the actual stage and cause.
- `TurnContentError`: targeted content reading failed; `failure` preserves the specific read or reconstruction cause.
- `TurnWriterStateError`: the writer is completed or unresolved.
- `UuidAllocationError`: Publish has not been sent; `failure` identifies the failed RPC or lifecycle cause.
- `PublishFailedError`: after obtaining the UUID, PublishAutoSeq or UUID lookup failed, or an initiated Publish was interrupted by the client lifecycle. Its `uuid` and `failure` can be logged. The caller cannot automatically send a new Chat message based only on its error category. The SDK's internal size-rejection conversion follows section 5.1 only.
- `ClientFailedError` and `ClientClosedError`: the client has permanently failed or closed.

## 7. Client lifecycle

After construction the client is ready and can only permanently fail or close. Failed or closed clients cannot recover or be reused. Invalid protocol messages, established contract contradictions, or corrupted derived local state permanently fail the client. Errors are reported through the current synchronous method's exception. Later client calls and bound writer writes raise `ClientFailedError` preserving the original `FailureInfo`. Ordinary remote errors from one-page or targeted reads end only that call; the caller decides whether to retry or exit.

```python
chat.close()
```

`close()` is idempotent and supports a context manager. It rejects new work, wakes retry waits, and waits for in-flight work, including bound writer calls, to finish. Bound writer writes after close raise `ClientClosedError`. Closing does not enumerate writers, cancel issued RPCs, or close the injected OpenEvent client.

The underlying connection is managed by its owner. To interrupt its in-flight RPCs, the owner calls `OpenEventClient.close()` according to the OpenEvent SDK's public close contract. Closing a shared connection affects all Chat clients using it. Cancelling an RPC does not prove that a remote write did not commit.

## 8. Deployment and limitations

Deploy at most one active Chat writing process per `(principal, channel_id)`. One process may hold writers for different turns. The SDK provides no cross-process lock, lease, persistent cursor, checkpoint, or application projection. Applications coordinate writers, persist the read positions and business recovery state they need, and arrange reads and processing after restart.

The SDK reconstructs terminal content only through section 3.3; it does not maintain conversation history or allocation state, or validate historical reservation capacity. Each message accepts at most 1024 ObjectKeys. SDK errors never log ObjectKey tokens, principal tokens, payloads, or full bodies.

The protocol retains `chat.v1`, but its end manifests, creation positions, and terminal reply rules are incompatible with old history. SDKs, Agents, Chat Server, browsers, and other readers and writers must update together. There is no compatibility mode or old-history migration; [protocol section 1](CHAT_PROTOCOL.md#1-protocol-boundary) defines the complete boundary.
