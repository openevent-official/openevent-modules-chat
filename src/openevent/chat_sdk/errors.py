"""Public errors. Remote error bodies never enter diagnostic messages."""
from dataclasses import dataclass
from typing import Literal

import grpc


@dataclass(frozen=True)
class FailureInfo:
    stage: str
    category: Literal["external_unavailable", "authentication", "permission",
                      "not_found", "request_rejected", "protocol", "contract", "lifecycle"]
    grpc_code: grpc.StatusCode | None
    detail: str


class ChatProtocolError(ValueError):
    pass


class _FailureError(Exception):
    def __init__(self, failure: FailureInfo):
        super().__init__(failure.detail)
        self._failure = failure

    @property
    def failure(self) -> FailureInfo:
        return self._failure


class ChannelInitializationError(_FailureError):
    pass


class FetchPageError(_FailureError):
    pass


class SyncReadError(_FailureError):
    pass


class UuidAllocationError(_FailureError):
    pass


class PublishFailedError(_FailureError):
    def __init__(self, failure: FailureInfo, uuid: int, *, uncertain: bool = True):
        super().__init__(failure)
        self.uuid = uuid
        self.uncertain = uncertain


class ClientFailedError(_FailureError):
    pass


class ClientClosedError(Exception):
    pass


class TurnNotFoundError(Exception):
    pass


class TurnWriterStateError(Exception):
    pass


def make_failure(stage, exc=None, *, attempts=1, category=None, detail=None):
    """Classify a failure without copying untrusted exception text."""
    if isinstance(exc, _FailureError):
        return exc.failure
    code = exc.code() if isinstance(exc, grpc.RpcError) else None
    if category is None:
        if ((stage == "PublishAutoSeq" and code in {
                grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED})
                or (stage == "WriteObject" and code == grpc.StatusCode.INVALID_ARGUMENT)):
            category = "request_rejected"
        elif code in {grpc.StatusCode.CANCELLED, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNKNOWN,
                    grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL,
                    grpc.StatusCode.RESOURCE_EXHAUSTED}:
            category = "external_unavailable"
        elif code == grpc.StatusCode.UNAUTHENTICATED:
            category = "authentication"
        elif code == grpc.StatusCode.PERMISSION_DENIED:
            category = "permission"
        elif code == grpc.StatusCode.NOT_FOUND:
            category = "not_found"
        elif isinstance(exc, ChatProtocolError):
            category = "protocol"
        elif isinstance(exc, ClientClosedError):
            category = "lifecycle"
        else:
            category = "contract"
    return FailureInfo(stage, category, code,
                       detail or f"{stage} failed after {attempts} attempts")
