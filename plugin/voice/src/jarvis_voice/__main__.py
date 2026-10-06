"""``python -m jarvis_voice``."""

import logging
import os
import sys

from .cli import main

if __name__ == "__main__":
    code = main()
    # Flush, then exit hard: keyboard hooks and PortAudio threads must not keep
    # an orphaned helper alive after the daemon decided to stop.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    logging.shutdown()
    os._exit(code)
