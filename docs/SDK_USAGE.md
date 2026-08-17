# Chat Python SDK Usage

[中文版](SDK_USAGE_cn.md)

## 1. Installation And Dependencies

`openevent-modules-chat` requires Python 3.10 or later and
`openevent-sdk>=0.4.4` already installed in the current Python environment.

```bash
make build
make install
```

Builds and tests do not install the SDK from the repository's
`openevent-sdk` submodule and do not generate proto modules at runtime.

## 2. Stateless Parsing

```python
from openevent.chat_sdk import TurnStart, parse_message, parse_payload

event = parse_payload(payload_bytes)
parsed = parse_message(event_message)

if isinstance(parsed.payload, TurnStart):
    print(parsed.turn_ref, parsed.payload.content)
```

`parse_payload` strictly requires UTF-8 JSON, unique object member names,
finite JSON numbers, and each event kind's exact field set. `parse_message`
also preserves `seq`, `ts_ms`, channel ID, publisher principal, recipients,
and ObjectKeys. ObjectKey tokens are always redacted from `repr` output.

## 3. Creating A Stateful Writer

Create a concurrency-safe `openevent.sdk.OpenEventClient`, then bind a fixed
identity and a non-empty channel set:

```python
from openevent.chat_sdk import create_client
from openevent.sdk import OpenEventClient

openevent_client = OpenEventClient("127.0.0.1:9527")
chat = create_client(
    openevent_client,
    principal=9001,
    token="...",
    channel_ids=[10001, 10002],
)
```

Initialization validates `protocol="chat.v1"` on every channel and blocks
while recovering to a fixed watermark. The Chat client does not own the
injected OpenEvent client or gRPC channel. Keep them open until after closing
the Chat client.

## 4. Publishing Turns

```python
from openevent.chat_sdk import ObjectKey, TextPart, TurnRef

start_seq = chat.start_turn(
    channel_id=10001,
    turn_id="turn-user-1",
    reply_to_turns=[],
    content=[TextPart("hello")],
    recipients=[],
    object_keys=[ObjectKey(7001, "object-token")],
    extensions={"ui": {"language": "en"}},
)

append_seq = chat.append_turn(
    channel_id=10001,
    turn_id="turn-user-1",
    content=[TextPart(" world")],
)

end_seq = chat.complete_turn(channel_id=10001, turn_id="turn-user-1")
cancel_seq = chat.cancel_turn(
    channel_id=10001,
    target_turn=TurnRef(principal=9002, turn_id="turn-agent-1"),
)
```

Before every publish, the calling thread obtains a fixed watermark `W` with
GetStatus, raises the client's shared monotonic `sync_target_seq` with
`max(sync_target_seq, W)`, and waits for the single internal sync thread to
reach its own `W` using Fetch. The SDK then selects the append/end `pre_seq`,
publishes, and returns only after the sync thread has processed the committed
seq. The sync thread performs Fetch and state updates only; it does not execute
GetStatus or PublishAutoSeq. One client permits at most one concurrent publish
for the same TurnRef; different TurnRefs may publish concurrently.

Applications continue to read, display, and process Chat events directly via
OpenEvent Fetch/Subscribe, then parse each message with `parse_message`.

## 5. Errors And Closing

- `ChatProtocolError` reports invalid call input or a malformed `chat.v1` payload.
- `TurnNotFoundError`, `TurnAlreadyExistsError`, and `TurnBusyError` report turn state known before publication.
- `SyncReadError.publish_sent` is always `False`: pre-publish synchronization failed before PublishAutoSeq was sent.
- `PublishFailedError.code` preserves the PublishAutoSeq gRPC status.
- `PublishCommittedSyncError.seq` means the message committed but ordered confirmation did not finish; do not treat it as uncommitted.
- `ClientFailedError` means the client has permanently fail-stopped and later stateful calls issue no network request.

Use the context manager or call `close()` explicitly. Closing does not close
the injected OpenEvent client. Concurrent and repeated `close()` calls are
idempotent.

## 6. Verification

```bash
make test
make build
```

Real end-to-end tests use only the `openevent-sdk` installed in the current
environment. Configure them before running:

```bash
export OPENEVENT_E2E_ADDR=127.0.0.1:9527
export OPENEVENT_E2E_PRINCIPAL=9001
export OPENEVENT_E2E_TOKEN=...
export OPENEVENT_E2E_CHAT_CHANNEL_ID=10001
make e2e
```

The channel must already exist, be fully readable by the identity, and use
`protocol="chat.v1"`.
