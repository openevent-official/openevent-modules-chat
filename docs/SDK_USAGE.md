# Chat Python SDK Quick Start

[中文版](SDK_USAGE_cn.md)

This guide is a minimal usage example. The complete public API, retry,
and lifecycle rules are in [CHAT_SDK.md](CHAT_SDK.md).

The examples below use the current SDK interfaces for creation-event seq replies,
independent writing objects, and explicit recovery.

## Requirements

Install `openevent-modules-chat` with its `openevent-sdk>=0.8.1` dependency.
Create a non-system OpenEvent channel whose protocol is `"chat.v1"` before
constructing the client.

## Create A Client

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

Deploy one active writing process for each `(principal, channel_id)`; different
turns can have their own writing objects. The SDK owns neither
the supplied OpenEvent client nor its gRPC channel. Construction does not read
history; callers read it one page at a time with `fetch_page(from_seq, limit)`.

## Write And Read

```python
user_creation_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("hello"),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(TextPart("hello"),),
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

A successful write confirms OpenEvent committed the message. The application
loop in this example reads from the history start, saves `next_seq` after
processing each page, and stops after reading past the reply's end event.
Applications also organize their own continuous read-and-process loops;
tail waits and stopping conditions follow the
[one-page read contract](CHAT_SDK.md#21-one-fetch-per-page).

Save the seq returned by `single_turn`, or the `creation_seq` of the object
returned by `start_turn`, then pass it in `reply_to_seqs` when replying.
The SDK validates field shape without
reading history to verify targets; callers select valid targets under
[protocol section 11](CHAT_PROTOCOL.md#11-replies).

For a streaming turn, use `start_turn` to obtain a writing object, then call
its `append()` zero or more times and finish with `complete()`. The object owns
its tail; the client keeps no turn state. Release the object after completion.
Use `cancel_turn` to record cancellation of an existing TurnRef. After normal
reads observe an effective cancellation, the caller stops writing and releases
the corresponding object. Release itself neither publishes an event nor revokes
an in-flight request. Continuing an old turn requires explicit `resume_turn()`;
see the [publishing API](CHAT_SDK.md#3-publishing-api) for its preconditions and
failure handling.
For files generated during streaming output, see the
[append-attachment example in the publishing API](CHAT_SDK.md#3-publishing-api).

## Close

```python
chat.close()
events.close()
```

The example waits for Chat calls to finish before releasing `events`. For interrupting
in-flight requests, see the
[lifecycle contract](CHAT_SDK.md#5-client-lifecycle). Failed methods raise exceptions; see the
[error contract](CHAT_SDK.md#4-uuids-retries-and-errors).
