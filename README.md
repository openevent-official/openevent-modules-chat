# OpenEvent Chat Module

[中文版本](README_cn.md)

OpenEvent Chat defines `chat.v1`, a compact event protocol for permanent conversations between users and Agents. A Channel stores turn events and submission-number reservation control events in an append-only history. Text can be appended as a model generates it, and an unfinished streaming turn can be reset and streamed again. Events for different turns can safely interleave.

> The Chat SDK, Chat Server, browser, and Agent must all support the current `chat.v1`, including streaming resets, empty-content starts, and attachment-only appends. Older reading rules are not supported.

The project includes a Python SDK for `chat.v1` writes, one-page-at-a-time Fetch reads, and stateless message parsing. Streaming messages are appended, reset, and ended through writer objects held by the caller; continuing an existing message requires explicit writer recovery. SDK construction does not recover history automatically. Callers that need continuous reads run their own Fetch loop and retain their read position.
The project also provides a chat backend and browser page with sessions, files, and active message polling; it does not include an Agent runtime or model integration.

## Protocol

Channels using this protocol must set:

```text
ChannelInfo.protocol = "chat.v1"
```

The protocol defines six turn event kinds and one submission-number reservation control event:

- `turn.single`: create and normally complete a turn in one event, carrying its complete text and reply references.
- `turn.start`: create a streaming turn with a writer-generated `turn_id`, allowing the turn to exist before content is produced; the owner principal and `turn_id` jointly identify the turn.
- `turn.append`: append text or attachments to an existing turn; see [protocol section 4](docs/CHAT_PROTOCOL.md#4-content-part) for the content constraints.
- `turn.reset`: replace the current visible content of an open streaming turn while preserving the same reply and its references; see [protocol section 15](docs/CHAT_PROTOCOL.md#15-turnreset) for the complete rules.
- `turn.end`: normally complete a turn.
- `turn.cancel`: independently cancel an existing turn.
- `submission.reserve`: record the current Channel's submission-number reservation upper bound without creating a turn; see [protocol section 14](docs/CHAT_PROTOCOL.md#14-submissionreserve) for the complete rules.

Replies reference the target turn's creation-event seq through `reply_to_seqs`; see [protocol section 11](docs/CHAT_PROTOCOL.md#11-reply-relationships) for the semantics.

`turn.cancel` only records a cancellation fact. Applications using the Chat SDK read effective cancellation events through `fetch_page()` and stop the corresponding model, tool, or other work themselves; the Chat protocol does not directly stop application tasks.

A turn's terminal state cannot be changed by later events; see [protocol section 10](docs/CHAT_PROTOCOL.md#10-chain-and-terminal-state-semantics) for the complete rules.

The complete public specification is in [docs/CHAT_PROTOCOL.md](docs/CHAT_PROTOCOL.md).

## Python SDK

The installable Python SDK is in `src/openevent/chat_sdk`. The public read and write APIs, state, retries, and lifecycle contract are defined in [docs/CHAT_SDK.md](docs/CHAT_SDK.md); see [docs/SDK_USAGE.md](docs/SDK_USAGE.md) for introductory examples.

## Browser application

The backend and static page ship in the same package. Run `make install`, then start it with `openevent-chat --config ./chat.json`.
See the [application usage guide](docs/APP_USAGE.md) for configuration, usage, and restart instructions.

## Boundaries

`chat.v1` intentionally does not define:

- whether a principal is a user or an Agent;
- Channel visibility or membership management policy;
- application semantics of OpenEvent `recipients`;
- Agent internal state, model requests, tools, hidden reasoning, or scheduling;
- business-level idempotency, retries, or handling of invalid history.

Applications may attach OpenEvent ObjectKeys to turn events and store user-visible application metadata in the protocol's `extensions` object.
The base protocol supports only text content parts and does not allow event kinds beyond the seven listed above.

## Runtime requirements

- An OpenEvent server supporting Channels, event history, and Fetch.
- The Python SDK requires `openevent-sdk>=0.11.1` installed in the current environment; the current reference SDK version is `0.11.1`.
- An OpenEvent server supporting message UUIDs, as defined in the OpenEvent API documentation.
- Direct SDK use requires a non-system Channel with `protocol="chat.v1"` created beforehand; the browser application creates Channels automatically.

Run `make check-sdk` before building or testing to confirm that the installed SDK is available. `make e2e` also requires `OPENEVENT_SERVER_BIN` to point to an executable OpenEvent server; it uses only the installed SDK and does not install the SDK from source.

OpenEvent stores events and enforces its own Channel ACLs; it does not parse or validate `chat.v1` JSON.
