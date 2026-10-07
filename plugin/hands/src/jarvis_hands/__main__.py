"""``python -m jarvis_hands``."""

import logging
import os
import sys

from . import lifecycle
from .cli import main

if __name__ == "__main__":
    code = main()
    # Exit hard: camera reader threads, the overlay window and MediaPipe's
    # native threads must not keep an orphaned helper (and the webcam) alive
    # after the runtime decided to stop. os._exit skips atexit, so the hooks
    # (the executor's releases any held mouse button) run first, time-boxed.
    lifecycle.run_exit_hooks(lifecycle.EXIT_HOOKS_TIMEOUT)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    logging.shutdown()
    os._exit(code)
