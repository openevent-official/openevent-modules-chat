# OpenEvent Chat Module

[中文版](README_cn.md)

OpenEvent Chat defines `chat.v1`, a small event protocol for persistent
conversations between users and agents. A channel stores append-only turn
events and submission reservation control events. Text can be appended incrementally so model output is visible while it
is being generated, and independent turns can be interleaved safely.

The repository includes a Python SDK for `chat.v1` writes, one-page
Fetch reads, and stateless message parsing. Callers hold writer objects to append
to and complete streaming turns, and explicitly recover a writer to continue an
existing turn. SDK construction does not recover history; callers loop over Fetch
and save their read positions when continuous reading is needed.
The project also includes a chat backend and browser page for sessions, files, and
client-initiated message polling. It does not include an Agent runtime or model integration.

## Protocol

Every channel using this protocol sets:

```text
ChannelInfo.protocol = "chat.v1"
```

The protocol defines five turn event kinds and one submission reservation control event kind:

- `turn.single`: creates and normally completes a turn in one event, carrying
  its complete text content and reply references.
- `turn.start`: creates a turn with a writer-supplied `turn_id` and carries its
  first text content. A turn is identified by owner principal plus `turn_id`.
- `turn.append`: appends text content to an existing turn.
- `turn.end`: normally completes a turn.
- `turn.cancel`: independently cancels an existing turn.
- `submission.reserve`: records the current channel's submission identifier
  reservation ceiling without creating a turn. See
  [protocol section 14](docs/CHAT_PROTOCOL.md#14-submissionreserve) for the complete rules.

Replies use `reply_to_seqs` to reference target turns by their creation-event
seq. See [protocol section 11](docs/CHAT_PROTOCOL.md#11-replies) for the semantics.

`turn.cancel` records a cancellation fact only. When using the Chat SDK,
applications read effective cancellation events with `fetch_page()` and stop
the corresponding model, tool, or other application work; the Chat protocol
does not directly interrupt application tasks.

Once a turn is terminal, later append, end, and cancel events targeting it do
not change the resolved terminal state, final content, or pre-terminal chain
tail, even when committed. Implementations need no special parsing or
validation order for these events.

See [docs/CHAT_PROTOCOL.md](docs/CHAT_PROTOCOL.md) for the complete public
specification.

## Python SDK

The installable Python SDK is under `src/openevent/chat_sdk`. Its public read and
write APIs, state, retry, and lifecycle contract is defined by
[docs/CHAT_SDK.md](docs/CHAT_SDK.md). Start with
[docs/SDK_USAGE.md](docs/SDK_USAGE.md).

## Browser application

The backend and static page ship in the same package. After `make install`, start
with `openevent-chat --config ./chat.json`. See the [application guide](docs/APP_USAGE.md)
for configuration, usage, and restart instructions.

## Boundary

`chat.v1` intentionally does not define:

- whether a principal is a user or an agent;
- channel visibility or membership policy;
- application semantics for OpenEvent `recipients`;
- agent internals, model requests, tools, hidden reasoning, or orchestration;
- business-level idempotency, retry, or invalid-history handling policy.

Applications may attach OpenEvent ObjectKeys to turn events and may store application-visible
metadata in the protocol's `extensions` object. The base protocol supports text
content parts only and does not permit event kinds beyond the six listed
above.

## Requirements

- An OpenEvent server supporting channels, event history, and Fetch.
- An installed `openevent-sdk>=0.8.1` for the Python SDK; the current reference
  SDK is `0.8.1`.
- An OpenEvent server that supports message UUIDs, as documented by the
  OpenEvent API.
- For direct SDK use, a pre-created non-system channel with `protocol="chat.v1"`;
  the browser application creates its Channels automatically.

Run `make check-sdk` to verify the installed SDK before building or running
tests. `make e2e` also requires `OPENEVENT_SERVER_BIN` to name an executable
OpenEvent server binary; it uses the installed SDK and never installs one from
source.

OpenEvent stores the events and enforces its own channel ACL. It does not parse
or validate `chat.v1` JSON.
