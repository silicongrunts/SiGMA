"""Keep a shell's process group attached to the web process on Linux."""

import ctypes
import os
import signal
import subprocess
import sys


def terminate_group(*_args) -> None:
    group_id = os.getpgrp()
    if group_id <= 1 or group_id != os.getpid():
        raise RuntimeError("Shell worker must own its process group")
    os.killpg(group_id, signal.SIGKILL)


def main() -> int:
    parent_pid = int(sys.argv[1])
    command = sys.argv[2]
    if sys.platform == "linux":
        signal.signal(signal.SIGTERM, terminate_group)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if os.getppid() != parent_pid:
            terminate_group()
    result = subprocess.call(command, shell=True)
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    sys.exit(main())
