# OpenEvent Chat Module

[中文版](README_cn.md)

OpenEvent Chat defines `chat.v1`, a small event protocol for persistent
conversations between users and agents. A channel stores append-only turn
events. Text can be appended incrementally so model output is visible while it
is being generated, and independent turns can be interleaved safely.

The repository includes a Python SDK for the stateful `chat.v1` writer and
Fetch-based subscription callback API. The project does not provide a worker,
agent runtime, model integration, or user interface.

## Protocol

Every channel using this protocol sets:

```text
ChannelInfo.protocol = "chat.v1"
```

The protocol defines five event kinds:

- `turn.single`: creates and normally completes a turn in one event, carrying
  its complete text content and reply references.
- `turn.start`: creates a turn with a writer-supplied `turn_id` and carries its
  first text content. A turn is identified by owner principal plus `turn_id`.
- `turn.append`: appends text content to an existing turn.
- `turn.end`: normally completes a turn.
- `turn.cancel`: independently cancels an existing turn.

`turn.cancel` records a cancellation fact only. When using the Chat SDK,
applications observe effective cancellation events through its subscription
callback and stop
the corresponding model, tool, or other application work; the Chat protocol
does not directly interrupt application tasks.

Once a turn is terminal, later append, end, and cancel events targeting it do
not change the resolved terminal state, final content, or pre-terminal chain
tail, even when committed. Implementations need no special parsing or
validation order for these events.

See [docs/CHAT_PROTOCOL.md](docs/CHAT_PROTOCOL.md) for the complete public
specification.

## Python SDK

The installable Python SDK is under `src/openevent/chat_sdk`. Its complete
state, retry, lifecycle, and subscription contract is defined by the internal
`CHAT_SDK_DESIGN.md` document.

## Boundary

`chat.v1` intentionally does not define:

- whether a principal is a user or an agent;
- channel visibility or membership policy;
- application semantics for OpenEvent `recipients`;
- agent internals, model requests, tools, hidden reasoning, or orchestration;
- business-level idempotency, retry, or invalid-history handling policy.

Applications may attach OpenEvent ObjectKeys and may store application-visible
metadata in the protocol's `extensions` object. The base protocol supports text
content parts only and does not permit event kinds beyond the five listed
above.

## Requirements

- An OpenEvent server supporting channels, event history, and Fetch.
- Direct protocol implementations require an OpenEvent server that supports
  message UUIDs, as documented by the OpenEvent API.
- A pre-created non-system channel with `protocol="chat.v1"`.

OpenEvent stores the events and enforces its own channel ACL. It does not parse
or validate `chat.v1` JSON.
