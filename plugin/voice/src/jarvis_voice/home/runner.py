"""One asyncio event loop on a daemon thread, for the async device libraries.

pyatv (and aiohttp under it) is asyncio-only while the helper is threaded:
the control server answers each request on its own thread. Those threads
hand coroutines to this loop and wait for the result with a timeout. The
loop starts on first use, so a helper that never controls a device never
starts it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncRunner:
    def __init__(self, name: str = "home-async") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The running loop, started on first use."""
        with self._lock:
            if self._closed:
                raise RuntimeError("the home event loop has been stopped")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def serve() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    try:
                        loop.run_forever()
                    finally:
                        loop.close()

                thread = threading.Thread(target=serve, name=self._name, daemon=True)
                thread.start()
                ready.wait(5)
                self._loop, self._thread = loop, thread
            return self._loop

    def in_loop_thread(self) -> bool:
        return self._thread is not None and threading.current_thread() is self._thread

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Schedules ``coro`` on the loop; the caller owns the future."""
        try:
            loop = self.loop
        except RuntimeError:
            coro.close()  # never awaited: close it so Python does not warn
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def call(self, coro: Coroutine[Any, Any, T], timeout: float) -> T:
        """Runs ``coro`` on the loop and waits up to ``timeout`` seconds.

        Raises TimeoutError (after cancelling the coroutine) when it takes
        longer; any exception the coroutine raises comes through as is.
        """
        if self.in_loop_thread():
            coro.close()
            raise RuntimeError("AsyncRunner.call from the loop's own thread would deadlock; await instead")
        future = self.submit(coro)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"no answer within {timeout:g} s") from None

    def stop(self, timeout: float = 5.0) -> None:
        """Cancels what is left on the loop, stops it and waits for its thread."""
        with self._lock:
            self._closed = True
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return

        async def drain() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=max(0.1, timeout - 1))

        try:
            asyncio.run_coroutine_threadsafe(drain(), loop).result(timeout)
        except Exception as exc:  # noqa: BLE001 - stopping is best effort
            log.debug("home event loop drain: %s", exc)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
