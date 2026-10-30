"""Shared requirements for the OpenEvent SDK installed in this interpreter."""

from importlib import metadata
import inspect
import re


MINIMUM_SDK_VERSION = (0, 13, 0)
SDK_REQUIREMENT = "openevent-sdk>=" + ".".join(map(str, MINIMUM_SDK_VERSION))
REQUIRED_METHODS = (
    "get_channel", "get_status", "fetch", "get_uuid", "get_seq_by_uuid",
    "publish_auto_seq", "create_channel", "write_object", "get_object_metadata",
    "batch_get_object_metadata", "read_object", "close",
)


def check_installed_sdk():
    """Check deployment capabilities before opening an OpenEvent connection."""
    try:
        installed = metadata.version("openevent-sdk")
        release = re.match(r"^(\d+)\.(\d+)\.(\d+)", installed)
        if not release or tuple(map(int, release.groups())) < MINIMUM_SDK_VERSION:
            raise RuntimeError(f"{SDK_REQUIREMENT} is required")
        from openevent.sdk import OpenEventClient
        from openevent.sdk.proto import openevent_pb2

        if "timeout_ms" not in inspect.signature(OpenEventClient).parameters:
            raise RuntimeError("installed openevent-sdk must expose timeout_ms")
        for name in REQUIRED_METHODS:
            if not callable(getattr(OpenEventClient, name, None)):
                raise RuntimeError(f"installed openevent-sdk is missing {name}")
        if "uuid" not in openevent_pb2.EventMessage.DESCRIPTOR.fields_by_name:
            raise RuntimeError("installed SDK protobuf does not support UUIDs")
        service = openevent_pb2.DESCRIPTOR.services_by_name.get("ObjectStorageService")
        batch = service.methods_by_name.get("BatchGetObjectMetadata") if service is not None else None
        if (batch is None or batch.client_streaming or batch.server_streaming
                or batch.input_type.full_name != "openevent.BatchGetObjectMetadataRequest"
                or batch.output_type.full_name != "openevent.BatchGetObjectMetadataResponse"):
            raise RuntimeError("installed SDK protobuf does not support BatchGetObjectMetadata")
        return installed
    except (metadata.PackageNotFoundError, ImportError) as exc:
        raise RuntimeError(f"Install {SDK_REQUIREMENT} in the current Python environment") from exc
