# Browser Chat Application Guide

[中文版](APP_USAGE_cn.md)

The application includes a Python backend and a browser page shipped in the package. Run OpenEvent separately; model and Agent services are supplied by the integrator. For Python SDK use alone, see the [SDK quick start](SDK_USAGE.md).

## Installation and verification

Use Python 3.10 or later, install `openevent-sdk>=0.8.1` in the current environment, then run from this repository:

```sh
make check-sdk
make install
```

Running `make install` again replaces the installed project with the current build. For a virtual environment, set `PYTHON` to that environment's Python interpreter.
That interpreter determines the installation destination; `make install` does not accept `--target`, `--prefix`, or `--root`. The isolated `make verify-wheel` check is unaffected.

When building and verifying through `make`, source copies, package metadata, caches, and temporary files stay under `build/`; final packages go to `dist/`. `make clean` removes these generated files.

Use `make test` for development checks; browser state tests require Node.js 22.12 or later. `make verify-wheel` rebuilds and verifies that the package includes the SDK, backend, and static page. For full integration testing:

```sh
OPENEVENT_SERVER_BIN=/opt/openevent/bin/openevent_server make e2e
```

End-to-end tests start temporary OpenEvent and Chat servers with temporary data directories. They use only the OpenEvent SDK already installed in the current Python environment, without installing or generating the SDK from its submodule.

With Playwright and Chromium installed, `make browser-e2e` also tests real browser interactions. Set `CHAT_CHROMIUM_BIN` to select an existing Chromium executable. `make check-docs` checks translation structure and local links.

## Configuration and startup

First use the OpenEvent administration API to assign distinct nonzero principals to the user and Agent and obtain their respective OpenEvent tokens. Give the Agent token only to the Agent service. Set a separate access token for the browser.

Create `chat.json` and protect its credentials with the deployment environment's access controls:

```json
{
  "openevent_target": "127.0.0.1:9527",
  "channels_dir": "./channels",
  "web_token": "replace-with-browser-access-token",
  "user_principal": "9001",
  "user_openevent_token": "replace-with-user-openevent-token",
  "agent_principal": "9002",
  "rpc_timeout_ms": 2000.0,
  "max_retries": 3
}
```

A relative `channels_dir` resolves from the configuration file's directory and is created automatically if missing. The deployer must restrict configuration file and directory access to the identity running the backend; the backend does not check operating-system permission bits. A cross-platform file lock allows only one process per directory. Configuration is read at startup without hot reload; configuration files and target directories must not be symbolic links.

```sh
openevent-chat --config ./chat.json --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/api/chat/`, enter `web_token`, and create a session. Its private Channel is created automatically; do not write session files by hand. The browser access token, OpenEvent user credentials, and Agent credentials serve different purposes and must not be interchanged.

Use an HTTPS reverse proxy in production and configure the public origin explicitly, for example:

```sh
openevent-chat --config ./chat.json --host 127.0.0.1 --port 8080 --origin https://chat.example.com
```

The proxy must forward `/api/chat/` and `/static/`. The browser uses a Secure cookie on HTTPS; local HTTP is for development. The token never appears in URLs. “Remove access token” clears the current page's chat records, drafts, and cookie.

## Using the page

- Create, select, or switch sessions. Only the selected session polls for messages; other sessions keep their page-local caches. Each browser tab works independently.
- Enter text or add files. Selecting a file uploads it immediately; all uploads must finish before sending. Each file must contain 1 byte to 4 MiB.
- Select existing messages as reply targets; multiple targets are supported. Streaming replies continue growing in their original positions.
- While an Agent is producing output, you can publish a cancellation event. If the result is unconfirmed, you can retry manually; the page does not resend automatically. The Agent service must read it and stop its own work.
- Messages in a session share file information. Failed loads can be retried manually. Files are downloaded, never executed or previewed in the chat page.
- When a send result is uncertain, the page queries its submission number or checks history. A network timeout does not mean the message was not committed.

Only fetched history creates formal chat records. Refreshing the page discards drafts and memory caches; committed chat history and files remain in OpenEvent.

## Agent integration

The Agent connects to OpenEvent using the configured `agent_principal` and its own credentials, then reads and writes `chat.v1` following the [SDK quick start](SDK_USAGE.md). The deployer obtains the Channel ID from session configuration; the browser never receives raw identities or Channel IDs. This project does not provide model invocation, Agent scheduling, or discovery services.

## Restart and failure handling

The session directory contains immutable session configuration. Do not edit, delete, or externally add session files. The service does not periodically recheck these files or Channel metadata; the deployer must keep them unchanged.

Publication failures or integrity errors stop the entire Chat Server with a nonzero exit status. Before restarting, operators must ensure that the old process and all its OpenEvent requests have ended and cannot commit later.

Transactions in `.pending/` that exactly match committed configuration are cleaned up automatically at startup. Creation transactions without committed configuration require operator inspection based on the reported error; the service never blindly creates a replacement Channel.

Local configuration is not protected against power loss. Creation request deduplication and recovery cover process exits while the operating system and filesystem remain operational. Power loss or an operating-system crash may lose local configuration or transaction records; operators must check session configuration against Channels in OpenEvent before restoring service. This does not change OpenEvent's own data guarantees.

Restarting discards in-memory credentials for unsent attachments. If the page still holds the local file, use “Upload again”; committed attachments remain readable through history. Submission numbers restart above the highest reserved upper bound recorded in OpenEvent history, without local number files.
