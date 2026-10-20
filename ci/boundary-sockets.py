"""Create filesystem socket fixtures; no listening server is needed for -S tests."""
import errno
import os
import socket
import sys

try:
    for path in sys.argv[1:]:
        with socket.socket(socket.AF_UNIX) as fixture:
            fixture.bind(path)
except OSError as error:
    # Like lint.sh, only a non-empty gate ID marks a trusted run.
    if error.errno in (errno.EPERM, errno.EACCES) and not os.environ.get("TASK_GATE_RUN_ID"):
        print(f"SKIP (sandbox: AF_UNIX fixture denied: {error}); socket boundary assertions only")
        sys.exit(77)
    raise
