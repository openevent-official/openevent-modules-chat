"""Check the installed SDK using this checkout's shared deployment rules."""
from pathlib import Path
import runpy
import sys


# Load only the helper: importing chat_app would also import the application and
# its dependencies. Never add an SDK checkout or an older installed Chat to sys.path.
check = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                          "src/openevent/chat_app/sdk_check.py"))["check_installed_sdk"]


if __name__ == '__main__':
    try:
        print(f'Installed openevent-sdk {check()} is ready')
    except RuntimeError as exc:
        sys.exit(str(exc))
