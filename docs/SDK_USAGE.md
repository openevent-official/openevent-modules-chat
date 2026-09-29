# Chat Python SDK Quick Start

[中文版](SDK_USAGE_cn.md)

This page provides minimal examples only. See [CHAT_SDK.md](CHAT_SDK.md) for the complete public API, retry, and lifecycle rules.

The examples use replies by creation event seq, independent writers, explicit recovery, empty-content starts, and resets.

## Prerequisites

Install `openevent-modules-chat` and its `openevent-sdk>=0.11.1` dependency. Before constructing a client, create a non-system Channel with `protocol="chat.v1"`.

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

A successful write return means OpenEvent committed the message. The example application loop reads from the history start one page at a time, saves `next_seq` after processing each page, and stops after passing the reply's end event. Applications also organize their own continuous read and processing loops. Tail waits and stopping conditions are covered by the [one-page read contract](CHAT_SDK.md#21-reading-one-page).

Save the seq returned by `single_turn`, or the `creation_seq` of a writer returned by `start_turn`, and pass it in `reply_to_seqs` when replying. The SDK validates field shape only; it does not read history to validate targets. Select valid targets according to [protocol section 11](CHAT_PROTOCOL.md#11-reply-relationships).

For a streaming turn, obtain a writer with `start_turn`, call its `append()` zero or more times, then call `complete()`. The writer keeps its own tail; the client keeps no turn state. Release the writer after completion. Use `cancel_turn` to record cancellation of an existing TurnRef. Once normal reads observe an effective cancellation, stop writing and release the writer. Releasing it neither publishes events nor revokes in-flight requests. Continuing an old turn requires explicit `resume_turn()`; see the [write API](CHAT_SDK.md#3-write-api) for preconditions and failure handling. For files generated during output, see the [attachment append example](CHAT_SDK.md#3-write-api).

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

Alternatively, `writer.reset(content=(TextPart("First retry chunk"),))` clears the old content and publishes the new first chunk in one event. A successful reset return means only that the event committed; it does not prove that the user has not cancelled. Applications may read back before retrying, but this is not a required synchronization barrier and cannot rule out a new cancellation after the read. Stop writing when normal reads observe an effective cancellation. The page still shows the same turn, with unchanged creation position and reply relationships. Replacement of content and attachments, cancellation races, unresolved publication, and recovery limits are defined only by the [public writer contract](CHAT_SDK.md#31-independent-writers). Reset cannot bypass an unresolved write or reopen a terminal turn.

## Closing

```python
chat.close()
events.close()
```

The example waits for Chat calls to finish before releasing `events`. See the [lifecycle contract](CHAT_SDK.md#5-client-lifecycle) for interrupting in-flight requests. Methods report failure through exceptions; see the [error contract](CHAT_SDK.md#4-uuids-retries-and-errors).
