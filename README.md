# OpenEvent Chat Module

[中文版](README_cn.md)

OpenEvent Chat defines `chat.v1`, a small event protocol for persistent
conversations between users and agents. A channel stores append-only turn
events. Text can be appended incrementally so model output is visible while it
is being generated, and independent turns can be interleaved safely.

The current project contains protocol documentation only. It does not provide
an SDK, worker, agent runtime, model integration, or user interface.

## Protocol

Every channel using this protocol sets:

```text
ChannelInfo.protocol = "chat.v1"
```

The protocol defines three event kinds:

- `turn.append`: creates a turn or appends text content to it.
- `turn.end`: normally completes a turn.
- `turn.cancel`: independently cancels an existing turn.

See [docs/CHAT_PROTOCOL.md](docs/CHAT_PROTOCOL.md) for the complete public
specification.

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
- OpenEvent `0.4.4` or later when ObjectKey attachments are used.
- A pre-created non-system channel with `protocol="chat.v1"`.

OpenEvent stores the events and enforces its own channel ACL. It does not parse
or validate `chat.v1` JSON.
