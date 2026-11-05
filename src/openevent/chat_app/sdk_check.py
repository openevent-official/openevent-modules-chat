"""Shared requirements for the OpenEvent SDK installed in this interpreter."""

from importlib import metadata


MINIMUM_SDK_VERSION = "0.13.0"
SDK_REQUIREMENT = f"openevent-sdk>={MINIMUM_SDK_VERSION}"


def check_installed_sdk():
    """Check the installed SDK version and imports before connecting."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:
        raise RuntimeError("Install the Chat package dependencies before checking the SDK (packaging is required)") from exc
    try:
        installed = metadata.version("openevent-sdk")
        try:
            release = Version(installed)
        except InvalidVersion as exc:
            raise RuntimeError(f"Installed SDK version is invalid; {SDK_REQUIREMENT} is required") from exc
        if release < Version(MINIMUM_SDK_VERSION):
            raise RuntimeError(f"{SDK_REQUIREMENT} is required")
        from openevent.sdk import OpenEventClient
        from openevent.sdk.proto import openevent_pb2
        return installed
    except (metadata.PackageNotFoundError, ImportError) as exc:
        raise RuntimeError(f"Install {SDK_REQUIREMENT} in the current Python environment") from exc
