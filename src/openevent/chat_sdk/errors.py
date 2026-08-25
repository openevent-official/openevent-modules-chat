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
    pass


class ChatProtocolError(ChatSdkError, ValueError):
    pass


class MalformedPayloadError(ChatProtocolError):
    pass


class InvalidKindError(ChatProtocolError):
    pass


class ChannelInitializationError(ChatSdkError):
    def __init__(self, cause: FailureCause | ChatSdkError, channel_id: int | None = None):
        self.channel_id = channel_id
        self.cause = cause
        subject = "channel initialization" if channel_id is None else f"channel {channel_id} initialization"
        super().__init__(f"{subject} failed: {cause}")


class ConversationStateError(ChatSdkError):
    pass


class HistoryConflictError(ConversationStateError):
    pass


class OpenEventContractError(ConversationStateError):
    pass


class TurnNotFoundError(ConversationStateError):
    def __init__(self, turn_ref: Any):
        self.turn_ref = turn_ref
        super().__init__(f"turn {turn_ref!r} does not exist")


class TurnAlreadyExistsError(ConversationStateError):
    def __init__(self, turn_ref: Any):
        self.turn_ref = turn_ref
        super().__init__(f"turn {turn_ref!r} already exists")


class TurnBusyError(ChatSdkError):
    def __init__(self, turn_ref: Any):
        self.turn_ref = turn_ref
        super().__init__(f"turn {turn_ref!r} already has a local publish in progress")


class SyncReadError(ChatSdkError):
    def __init__(self, cause: FailureCause | ChatSdkError):
        self.publish_sent = False
        self.cause = cause
        super().__init__(f"pre-publish synchronization failed: {cause}")


class UuidAllocationError(ChatSdkError):
    def __init__(self, cause: FailureCause | ChatSdkError):
        self.publish_sent = False
        self.cause = cause
        super().__init__(f"UUID allocation failed: {cause}")


class PublishFailedError(ChatSdkError):
    def __init__(self, code: Any, stage: str = "PublishAutoSeq"):
        self.code = code
        self.stage = stage
        super().__init__(f"{stage} failed with status {_code_name(code)}")


class ClientFailedError(ChatSdkError):
    def __init__(self, cause: FailureCause | ChatSdkError):
        self.cause = cause
        super().__init__(f"Chat client has permanently failed: {cause}")


class ClientClosedError(ChatSdkError):
    def __init__(self):
        super().__init__("Chat client is closing or closed")


class SubscriptionAlreadyRegisteredError(ChatSdkError):
    pass


class SubscriptionClosedError(ChatSdkError):
    pass


class SubscriptionError(ChatSdkError):
    pass


class SubscriptionCallbackError(SubscriptionError):
    pass


class SubscriptionProtocolError(SubscriptionError):
    pass
