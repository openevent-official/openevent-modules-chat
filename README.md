# OpenEvent Chat Module

[中文版](README_cn.md)

OpenEvent Chat defines `chat.v1`, a small event protocol for persistent
conversations between users and agents. A channel stores append-only turn
events. Text can be appended incrementally so model output is visible while it
is being generated, and independent turns can be interleaved safely.

The project also provides the `openevent.chat_sdk` Python SDK for strict
`chat.v1` parsing, turn write-state recovery, and safe construction of
start/append/end/cancel events. It does not provide a worker, agent runtime,
model integration, or user interface.

## Protocol

Every channel using this protocol sets:

```text
ChannelInfo.protocol = "chat.v1"
```

The protocol defines four event kinds:

- `turn.start`: creates a turn with a writer-supplied `turn_id` and carries its
  first text content. A turn is identified by owner principal plus `turn_id`.
- `turn.append`: appends text content to an existing turn.
- `turn.end`: normally completes a turn.
- `turn.cancel`: independently cancels an existing turn.

`turn.cancel` records a cancellation fact only. Applications should observe
effective cancellation events through their own Fetch/Subscribe path and stop
the corresponding model, tool, or other application work; the Chat protocol
does not directly interrupt application tasks.

Once a turn is terminal, later append, end, and cancel events targeting it do
not change the resolved terminal state, final content, or pre-terminal chain
tail, even when committed. Implementations need no special parsing or
validation order for these events.

See [docs/CHAT_PROTOCOL.md](docs/CHAT_PROTOCOL.md) for the complete public
specification.

## Python SDK

The SDK requires Python 3.10 or later and `openevent-sdk>=0.4.4` installed in
the current environment. A minimal writer looks like this:

```python
from openevent.chat_sdk import TextPart, create_client

with create_client(
    openevent_client,
    principal=9001,
    token="...",
    channel_ids=[10001],
) as chat:
    seq = chat.start_turn(
        channel_id=10001,
        turn_id="turn-01JABC",
        reply_to_turns=[],
        content=[TextPart("hello")],
    )
```

See [docs/SDK_USAGE.md](docs/SDK_USAGE.md) for installation, parsing,
publishing, failure-state, and concurrency details.

## Boundary

`chat.v1` intentionally does not define:

- whether a principal is a user or an agent;
- channel visibility or membership policy;
- application semantics for OpenEvent `recipients`;
- agent internals, model requests, tools, hidden reasoning, or orchestration;
- idempotency, retry, or invalid-history handling policy.

Applications may attach OpenEvent ObjectKeys and may store application-visible
metadata in the protocol's `extensions` object. The base protocol supports text
content parts only and does not permit additional event kinds.

## Requirements

- An OpenEvent server supporting channels, event history, and subscriptions.
- The Python SDK requires `openevent-sdk>=0.4.4` installed in the current
  environment. Direct protocol implementations also require OpenEvent `0.4.4`
  or later when ObjectKey attachments are used.
- A pre-created non-system channel with `protocol="chat.v1"`.

OpenEvent stores the events and enforces its own channel ACL. It does not parse
or validate `chat.v1` JSON.
