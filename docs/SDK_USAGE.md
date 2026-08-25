# Chat Python SDK

[中文版](SDK_USAGE_cn.md)

## 1. Implementation Status

This repository contains the installable Python SDK, its build entry point, and
unit tests together with the public `chat.v1` protocol.

The target implementation uses Python 3.10 or later and an installed
`openevent-sdk>=0.6.0`. It must use the public OpenEvent client and must not
generate protobuf modules or install an SDK from a source submodule.

## 2. Protocol Parsing

Applications that implement the protocol directly must validate each payload
according to [CHAT_PROTOCOL.md](CHAT_PROTOCOL.md), preserve the OpenEvent
`seq` order, and treat `EventMessage.uuid` as the OpenEvent message
deduplication identifier. OpenEvent does not parse or validate `chat.v1` JSON.

## 3. SDK Boundary

The SDK is a stateful writer for `chat.v1`; it does not provide a worker,
agent runtime, model integration, UI, or application-level authorization. Its
public API will not expose message UUID allocation. The injected OpenEvent
client owns the local UUID pool, and the SDK will obtain one UUID internally
for each actual publish.

The SDK also provides a subscription callback registration API. Applications
receive seq-ordered `ParsedMessage` values through Chat SDK. The SDK polls
with Fetch and never calls OpenEvent `Subscribe`; at the observed tail it
continues polling with backoff, and unary RPC failures use the common retry
rules.

```python
from openevent.sdk import OpenEventClient
from openevent.chat_sdk import create_client

events = OpenEventClient("127.0.0.1:50051", timeout=1.0)
chat = create_client(
    events,
    principal=9001,
    token="...",
    channel_id=10001,
    on_failure=lambda error: print(error),
)
```

Target usage:

```python
subscription = chat.register_subscription_callback(
    on_message,
    from_seq=1,
    on_error=on_subscription_error,
)

subscription.close()
```

`from_seq=0` (the default) uses the watermark returned by the first Fetch
linearization and receives only messages created after it; it does not promise
to cover all history that existed when registration was called. Use an explicit
starting point (normally `1`) when existing history must be covered. A value
greater than zero receives history and subsequent messages from that sequence through Fetch.
`SubscriptionHandle.wait_until_scanned(seq)` waits until Fetch has advanced
the returned `next_seq` beyond the requested watermark.

The Chat SDK client must be created with an `on_failure` callback. The SDK
invokes it once when a client that reached READY permanently enters FAILED
because of an internal Fetch failure, subscription Fetch failure, or protocol
state failure. Initialization errors, one-off publish errors, and `close()` do
not invoke it. The callback is notification only and cannot recover the client;
callers that do not need the notification must still pass an explicit no-op
callback. Callbacks run synchronously without an SDK timeout and must return
within a bounded time.

The complete synchronization, recovery, lifecycle, and error rules are part of
the package's published implementation contract.

## 4. Requirements

- An OpenEvent server with `chat.v1` channels, event history, and message UUID support.
- A pre-created non-system channel with `protocol="chat.v1"`.
- An installed `openevent-sdk>=0.6.0` when using the future Python SDK.
