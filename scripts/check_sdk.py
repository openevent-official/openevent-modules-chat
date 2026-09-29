"""Fail early: integration tests use the SDK installed in this interpreter."""
from importlib.metadata import PackageNotFoundError, version
import re
import inspect
import sys


def check():
    try:
        installed = version('openevent-sdk')
        parts = re.match(r'^(\d+)\.(\d+)\.(\d+)', installed)
        if not parts or tuple(map(int, parts.groups())) < (0, 11, 1):
            raise RuntimeError('openevent-sdk>=0.11.1 is required')
        from openevent.sdk import OpenEventClient
        from openevent.sdk.proto import openevent_pb2
        if 'timeout_ms' not in inspect.signature(OpenEventClient).parameters:
            raise RuntimeError('installed openevent-sdk must expose timeout_ms')
        for name in ('get_channel', 'get_status', 'fetch', 'get_uuid', 'get_seq_by_uuid',
                     'publish_auto_seq', 'create_channel', 'write_object',
                     'get_object_metadata', 'read_object', 'close'):
            if not callable(getattr(OpenEventClient, name, None)):
                raise RuntimeError(f'installed openevent-sdk is missing {name}')
        if 'uuid' not in openevent_pb2.EventMessage.DESCRIPTOR.fields_by_name:
            raise RuntimeError('installed SDK protobuf does not support UUIDs')
        return installed
    except (PackageNotFoundError, ImportError) as exc:
        raise RuntimeError('Install openevent-sdk>=0.11.1 in the current Python environment') from exc


if __name__ == '__main__':
    try:
        print(f'Installed openevent-sdk {check()} is ready')
    except RuntimeError as exc:
        sys.exit(str(exc))
