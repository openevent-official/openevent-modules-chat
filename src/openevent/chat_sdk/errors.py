from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _code_name(code: Any) -> str:
    return str(getattr(code, "name", code if code is not None else "UNKNOWN"))


@dataclass(frozen=True)
class FailureCause:
    stage: str
    code: Any = None
    detail: str = "operation failed"

    def __str__(self) -> str:
        suffix = f" ({_code_name(self.code)})" if self.code is not None else ""
        return f"{self.stage}: {self.detail}{suffix}"


class ChatSdkError(Exception):
    """Base class for public Chat SDK failures."""


class ChatProtocolError(ChatSdkError, ValueError):
    """A value does not satisfy the chat.v1 single-event contract."""


class MalformedPayloadError(ChatProtocolError):
    pass


class InvalidKindError(ChatProtocolError):
    pass


class ChannelNotManagedError(ChatSdkError):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        super().__init__(f"channel {channel_id} is not managed by this client")


class ChannelInitializationError(ChatSdkError):
    def __init__(self, channel_id: int | None, cause: FailureCause | ChatSdkError):
        self.channel_id = channel_id
        self.cause = cause
        subject = "channel initialization" if channel_id is None else f"channel {channel_id} initialization"
        super().__init__(f"{subject} failed: {cause}")


class ConversationStateError(ChatSdkError):
    pass


class HistoryConflictError(ConversationStateError):
    pass


class TurnNotFoundError(ConversationStateError):
    def __init__(self, channel_id: int, turn_ref: Any):
        self.channel_id = channel_id
        self.turn_ref = turn_ref
        super().__init__(f"turn {turn_ref!r} does not exist in channel {channel_id}")


class TurnAlreadyExistsError(ConversationStateError):
    def __init__(self, channel_id: int, turn_ref: Any):
        self.channel_id = channel_id
        self.turn_ref = turn_ref
        super().__init__(f"turn {turn_ref!r} already exists in channel {channel_id}")


class TurnBusyError(ChatSdkError):
    def __init__(self, channel_id: int, turn_ref: Any):
        self.channel_id = channel_id
        self.turn_ref = turn_ref
        super().__init__(f"turn {turn_ref!r} already has a local publish in progress in channel {channel_id}")


class PublishFailedError(ChatSdkError):
    def __init__(self, code: Any, channel_id: int, turn_ref: Any):
        self.code = code
        self.channel_id = channel_id
        self.turn_ref = turn_ref
        super().__init__(f"publish failed with status {_code_name(code)} for channel {channel_id}, turn {turn_ref!r}")


class SyncReadError(ChatSdkError):
    def __init__(self, cause: FailureCause | ChatSdkError):
        self.publish_sent = False
        self.cause = cause
        super().__init__(f"pre-publish synchronization failed: {cause}")


class PublishCommittedSyncError(ChatSdkError):
    def __init__(self, seq: int, cause: FailureCause | ChatSdkError):
        self.seq = seq
        self.cause = cause
        super().__init__(f"message {seq} committed but synchronization failed: {cause}")


class ClientFailedError(ChatSdkError):
    def __init__(self, cause: FailureCause | ChatSdkError):
        self.cause = cause
        super().__init__(f"Chat client has permanently failed: {cause}")


class ClientClosedError(ChatSdkError):
    def __init__(self):
        super().__init__("Chat client is closing or closed")

