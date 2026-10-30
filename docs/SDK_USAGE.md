# Chat Python SDK Quick Start

[中文版](SDK_USAGE_cn.md)

This page provides minimal examples only. See [CHAT_SDK.md](CHAT_SDK.md) for the complete public API, retry, and lifecycle rules.

The examples use replies by creation event seq, independent writers, continuing turns from known state, empty-content starts, and resets.

## Prerequisites

Install `openevent-modules-chat` and its `openevent-sdk>=0.13.0` dependency. Before constructing a client, create a non-system Channel with `protocol="chat.v1"`.

## Creating a client

```python
from openevent.sdk import OpenEventClient
from openevent.chat_sdk import TextPart, create_client

events = OpenEventClient("127.0.0.1:50051", timeout_ms=1000.0)
chat = create_client(
    events,
    principal=9001,
    token="...",
    channel_id=10001,
)
```

Deploy only one active writing process per `(principal, channel_id)`; different turns can each have their own writer. The SDK does not own the injected OpenEvent client or its gRPC channel. Construction does not read history. Callers needing history use `fetch_page(from_seq, limit)` one page at a time.

## Writing and reading

```python
user_creation_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("Hello"),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(TextPart("Hello"),),
)
reply_creation_seq = writer.creation_seq
writer.append(content=(TextPart(", nice to meet you."),))
reply_end_seq = writer.complete()
del writer

fetch_seq = 1
while True:
    page = chat.fetch_page(from_seq=fetch_seq, limit=100)
    for message in page.messages:
        print(message.seq, message.payload)
    fetch_seq = page.next_seq
    if fetch_seq > reply_end_seq:
        break
```

A successful write return means OpenEvent committed the message. The example application loop reads from the history start one page at a time, saves `next_seq` after processing each page, and stops after passing the reply's end event. The SDK has already excluded OpenEvent system messages; continue using the returned cursor even if the page contains no Chat messages. Applications also organize their own continuous read and processing loops. Tail waits and stopping conditions are covered by the [one-page read contract](CHAT_SDK.md#32-reading-one-page).

Save the seq returned by `single_turn`, or the `creation_seq` of a writer returned by `start_turn`, and pass it in `reply_to_seqs` when replying after confirming the target is terminal. The SDK validates field shape only; it does not read history to validate targets. Select valid targets according to [protocol section 12](CHAT_PROTOCOL.md#12-reply-relationships).

For a streaming turn, obtain a writer with `start_turn`, call its `append()` zero or more times, then call `complete()`. The writer keeps its own tail; the client keeps no turn state. Release the writer after completion. Use `cancel_turn` to record cancellation of an existing TurnRef. Once normal reads observe an effective cancellation, stop writing and release the writer. Releasing it neither publishes events nor revokes in-flight requests. For files generated during output, see the [attachment append example](CHAT_SDK.md#5-write-api).

## Continuing an unfinished turn

When an application's own reader has confirmed that a turn is unfinished and knows its creation position and current tail, it can construct a writer directly:

```python
from openevent.chat_sdk import TurnWriter

# The application projection confirmed this turn's state, and the old-write handoff is complete.
writer = TurnWriter(chat, turn_id="unfinished-1", creation_seq=210, last_seq=217)
writer.append(content=(TextPart("Continuing the previous reply."),))
writer.complete()
```

Construction does not reread history or publish another `turn.start`. See the [writer contract](CHAT_SDK.md#51-independent-writers) for parameter semantics. Applications reconstruct the current state. Old requests must finish before continuation; see the [old-write handoff contract](CHAT_SDK.md#52-old-write-handoff).

## Resetting unfinished output

After a model stream is interrupted, first stop accepting old model output and wait for all old Chat writes to be confirmed. Then reset through the same writer and publish retry output.
The example first creates a cancellable turn with empty content, then appends model output. See [protocol section 4](CHAT_PROTOCOL.md#4-content-part) for the complete empty-content conditions.

```python
writer = chat.start_turn(
    turn_id="retryable-reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(),
)
writer.append(content=(TextPart("Partial content before the interruption"),))
# Old model output scheduling has stopped; all old Chat writes are confirmed; the local writer remains open.
writer.reset()
# Retry output may continue directly; stop writing when normal reads observe an effective cancellation.
writer.append(content=(TextPart("New content from the retry"),))
writer.complete()
```

Alternatively, `writer.reset(content=(TextPart("First retry chunk"),))` clears the old content and publishes the new first chunk in one event. A successful reset return means only that the event committed; it does not prove that the user has not cancelled. Applications may read back before retrying, but this is not a required synchronization barrier and cannot rule out a new cancellation after the read. Stop writing when normal reads observe an effective cancellation. The page still shows the same turn, with unchanged creation position and reply relationships. See [protocol section 8](CHAT_PROTOCOL.md#8-turnreset) for content and attachment replacement, and [protocol section 11](CHAT_PROTOCOL.md#11-chain-and-terminal-state-semantics) for cancellation races and terminal states. Unresolved publication and recovery limits are defined by the SDK's [writer contract](CHAT_SDK.md#51-independent-writers) and [old-write handoff](CHAT_SDK.md#52-old-write-handoff). Reset cannot bypass an unresolved write or reopen a terminal turn.

## Carrying extensions unchanged

Use `RawExtensions` to forward extensions; ordinary application-generated extensions may still be supplied as dictionaries. See the
[raw extensions and serialization contract](CHAT_SDK.md#4-data-representation-and-serialization) for the interfaces and parsed result types.

```python
from openevent.chat_sdk import RawExtensions, serialize_payload

chat.single_turn(
    turn_id="with-metadata-1",
    content=(TextPart("A message with application metadata"),),
    extensions=RawExtensions('{"value":1e400}'),
)

# message is one ParsedMessage returned by fetch_page.
payload_bytes = serialize_payload(message.payload)
raw_extensions = message.payload.get("extensions")
if raw_extensions is not None:
    extension_json = raw_extensions.json_text
```

`payload_bytes` preserves the extension source text; do not first decode `extension_json` with an ordinary JSON parser just to forward it.
Applications choose an appropriate parser only when they need to read extension contents.

## Closing

```python
chat.close()
events.close()
```

The example waits for Chat calls to finish before releasing `events`. See the [lifecycle contract](CHAT_SDK.md#7-client-lifecycle) for interrupting in-flight requests. Methods report failure through exceptions; see the [error contract](CHAT_SDK.md#6-uuids-retries-and-errors).
