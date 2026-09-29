# Browser Chat Application Usage Guide

[中文版本](APP_USAGE_cn.md)

The application includes a Python backend and a browser page shipped with the package. Run OpenEvent separately; the integrating application provides model and Agent services. For Python SDK use alone, see the [SDK quick start](SDK_USAGE.md).

## Installation and verification

Use Python 3.10 or later, install `openevent-sdk>=0.11.1` in the current environment, then run in this repository:

```sh
make check-sdk
make install
```

Running `make install` again updates the installed project to the current build. When using a virtual environment, set `PYTHON` to that environment's Python interpreter.
The interpreter determines the installation destination. `make install` does not accept `--target`, `--prefix`, or `--root`; this does not affect the isolated verification performed by `make verify-wheel`.

Build and test output goes under `build/`, and packages go under `dist/`; `make clean` removes these files.

Use `make test` for development verification; browser state tests require Node.js 22.12 or later. `make verify-wheel` rebuilds and verifies that the package contains the SDK, backend, and static page. For a full integration test, run:

```sh
OPENEVENT_SERVER_BIN=/opt/openevent/bin/openevent_server make e2e
```

The end-to-end test starts temporary OpenEvent and Chat Server processes with temporary data directories.

With Playwright and Chromium installed, `make browser-e2e` also verifies real browser interactions. Set `CHAT_CHROMIUM_BIN` to use an existing Chromium executable. `make check-docs` checks translation structure and local links.

## Configuration and startup

The external application or deployment specifies distinct nonzero principals for the user and Agent. The Chat module uses the configured principals; it does not generate, allocate, or choose identities.
The deployment provides OpenEvent tokens for those specified principals. To create a token, call `AddToken` through the OpenEvent management API with the already specified principal.
Give the Agent token only to the Agent service. Set a separate browser access token.

Create `chat.json` and protect its credentials through deployment access controls:

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

A relative `channels_dir` is resolved from the configuration file's directory and is created automatically if absent. The deployment must ensure that only the backend's operating identity can read and write the configuration file and directory, with one process per directory. Restart after changing configuration; neither the configuration file nor the target directory may be a symbolic link.

```sh
openevent-chat --config ./chat.json --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/api/chat/`, enter `web_token`, and create a session. Each session automatically creates its private Channel; no manual session file is needed. The browser access token, OpenEvent user credentials, and Agent credentials serve three distinct purposes and must not be substituted for one another.

In production, access the application through an HTTPS reverse proxy and explicitly configure the external origin, for example:

```sh
openevent-chat --config ./chat.json --host 127.0.0.1 --port 8080 --origin https://chat.example.com
```

The proxy must forward `/api/chat/` and `/static/`. The browser uses a Secure Cookie over HTTPS; local HTTP is for development only. The token does not appear in URLs. “Remove access token” clears the chat records, drafts, and Cookie saved by the current page.

## Using the page

- Create, select, or switch sessions. Only the selected session updates messages automatically. Each tab works independently.
  On desktop and mobile, select Refresh to update the session list or retry a failed list request without clearing the current draft.
- Text input, uploads, sends, and cancellation become available after session initialization. History can continue loading during initialization; failures display the cause and allow initialization to be retried.
- Enter text and optionally add files. Selected files upload immediately and must finish uploading before sending; each file must be between 1 byte and 4 MiB.
- A file or its information failing validation rejects only that upload and does not stop the entire service.
- Select existing messages as reply targets; multiple targets are supported. Streaming replies append in place. When retrying, the Agent can reset the same reply and replace its interrupted content with new output.
- A reply still being generated remains visibly in progress and cancellable even with no text or after being reset to empty. Resetting does not change its position or existing references; reply previews update with the new content.
- Select Cancel while the Agent is producing output. If cancellation is unconfirmed, retry manually; the page does not resend automatically. Stopping the actual work requires support from the Agent service.
- Retry failed file-information loads manually. Files open through downloads and are neither executed nor previewed in the chat page.
- Ordinary requests wait at most 30 seconds; session preparation, uploads, and downloads wait at most 120 seconds. Message synchronization and session preparation retry temporary network failures and timeouts automatically.
  Session lists, session creation, file information, uploads, downloads, and cancellation require manual retries. If session creation fails, use the page's retry action to continue. A download is handed to the browser for saving only after it finishes completely; failures do not save a partial file.
- Selecting Send immediately locks the session's text, attachments, and reply selections. Until the outcome is confirmed, they cannot be edited and another message cannot be sent.
  If a send times out or is still processing, the page automatically checks the original message's outcome. A timeout does not mean the message was not sent. The content stays locked while the outcome is unknown; follow any prompt to confirm it manually.
  Confirmed success clears the submitted content and restores editing. Confirmation that nothing was sent preserves the content and restores editing; use the ordinary Send button to send it again.
  If the backend explicitly reports that an attachment is unavailable, the page ends that send, preserves the content, and restores editing even if an earlier request timed out. Upload the attachment again before sending again.
  Switching away pauses automatic confirmation; switching back resumes it. If the page has reported an error and stopped automatic confirmation, follow its instructions.

Refreshing loses drafts; sent chat records and files remain in OpenEvent.
A reset changes only the currently displayed text and attachments. Earlier events and their files remain in the underlying history; reset neither deletes records nor revokes file access.

## Agent integration

The Agent connects to OpenEvent using the configured `agent_principal` and its own credentials, and reads and writes `chat.v1` as described in the [SDK quick start](SDK_USAGE.md). The deployment obtains the corresponding Channel ID from the session configuration. This project does not provide model calls, Agent task scheduling, or a discovery service.

The Agent publishes streaming resets through the [Chat SDK](CHAT_SDK.md); see [turn.reset](CHAT_PROTOCOL.md#15-turnreset) for the protocol semantics.
The browser has no reset button. The Agent service detects model failures and handles retries.

## Restarts and failure handling

Do not edit, delete, or externally add session files, or change the corresponding Channel configuration.

When an OpenEvent call that permits retries exhausts `max_retries`, the entire Chat Server exits with a nonzero status, including failures while reading messages or attachments.
If the backend cannot determine whether OpenEvent created the Channel or stored the file, it exits without automatically repeating the write. Message publish failures and data integrity errors also cause the service to exit.
The page may continue automatic requests while waiting for service recovery. Before restarting, operators must ensure that the old process has exited and every old OpenEvent request has finished, with no possibility of a late commit.

After an ordinary process restart, the service automatically completes unfinished session creation when its saved information is complete. If an error reports that a pending record lacks a Channel ID, is corrupt, or conflicts with another record, operators must investigate and resolve it before restarting.
Automatic recovery requires the operating system and filesystem to remain healthy; local configuration does not provide power-loss protection. After power loss or an operating-system crash, operators must reconcile session configuration with OpenEvent Channels before resuming service.

After a restart, attachments that have not been sent need to be uploaded again. If the page retains the local file, select “Upload again”; otherwise, select the file again. Sent attachments remain readable.
