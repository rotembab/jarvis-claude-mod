"""The helper's one clock for frame times, gestures, filters and the executor.

``time.perf_counter``, not ``time.monotonic``: on Windows, Python 3.12's
``monotonic`` is GetTickCount64 and moves in 15.6 ms steps, which would
make a 30 fps camera's frame intervals read 31 or 47 ms and pair up the
executor's 120 Hz ticks. ``perf_counter`` is QueryPerformanceCounter there:
monotonic too, and sub-microsecond. Everything that compares times reads
this one clock, so camera stamps, the engine's ``now`` and the executor's
deadlines all line up.
"""

from __future__ import annotations

import time

now = time.perf_counter
