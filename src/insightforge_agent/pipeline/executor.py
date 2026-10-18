"""Where Runs execute: a fixed pool of worker threads in the API process (design.md section 11).

Two kinds of work, with different admission rules:

- A **new Run** goes through `admit`. Capacity (`max_workers` running plus `max_queued`
  waiting) is reserved *before* the Run is created, so a full executor refuses with
  `ExecutorFull` and no Run exists. Once created, the Run is queued and is never refused for
  capacity again.
- **Already accepted work** (a Run resuming after its clarification was resolved) goes through
  `submit`. It is never refused for capacity, because the Run is durably RESUMING and refusing
  would only strand it; it waits in the queue for a free worker.

Either kind is refused with `ExecutorClosed` once the executor is closed: `close` (as soon as
the process is asked to stop) or `shutdown`. Work that was queued but
never started when the executor shuts down is cancelled, and its `on_cancel` runs, so its Run
ends in an explicit state instead of disappearing. Running work is waited for: the process's
resources stay open until it has finished."""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class WorkNotAccepted(Exception):
    """The executor did not take the work."""


class ExecutorFull(WorkNotAccepted):
    """Every worker is busy and the queue for new Runs is full. The API answers 503."""


class ExecutorClosed(WorkNotAccepted):
    """The executor is shutting down."""


class RunExecutor(Protocol):
    def admit(self, create: Callable[[], T], work: Callable[[T], object],
              on_cancel: Callable[[T], object]) -> T: ...
    def submit(self, work: Callable[[], object],
               on_cancel: Callable[[], object] | None = None) -> Future: ...
    def shutdown(self) -> None: ...


class BoundedRunExecutor:
    def __init__(self, max_workers: int, max_queued: int = 0) -> None:
        if max_workers < 1 or max_queued < 0:
            raise ValueError("a RunExecutor needs at least one worker and a queue of 0 or more")
        self.max_workers = max_workers
        self.capacity = max_workers + max_queued  # new Runs admitted only below this
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="run")
        self._lock = threading.Lock()
        self._in_flight = 0  # queued and running work of either kind
        self._closed = False

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def admit(self, create: Callable[[], T], work: Callable[[T], object],
              on_cancel: Callable[[T], object]) -> T:
        """Reserve a place, then `create()` (which makes the Run durable), then queue
        `work(created)`. Raises ExecutorFull or ExecutorClosed before `create` runs, so a
        refused Run never exists; an exception from `create` releases the place and propagates.
        If shutdown begins between `create` and queueing, `on_cancel(created)` settles the Run
        and ExecutorClosed is raised."""
        with self._lock:
            if self._closed:
                raise ExecutorClosed("the executor is shutting down")
            if self._in_flight >= self.capacity:
                raise ExecutorFull(f"{self._in_flight} Runs are already running or queued "
                                   f"(capacity {self.capacity})")
            self._in_flight += 1
        try:
            created = create()
        except BaseException:
            self._release()
            raise
        self._enqueue(lambda: work(created), lambda: on_cancel(created), reserved=True)
        return created

    def submit(self, work: Callable[[], object],
               on_cancel: Callable[[], object] | None = None) -> Future:
        """Queue already accepted work. Refused only once shutdown has begun (ExecutorClosed,
        before anything runs: `on_cancel` is not called then, the caller handles it)."""
        return self._enqueue(work, on_cancel, reserved=False)

    def _enqueue(self, work: Callable[[], object], on_cancel: Callable[[], object] | None,
                 *, reserved: bool) -> Future:
        with self._lock:
            closed = self._closed
            if not closed:
                if not reserved:
                    self._in_flight += 1
                future = self._pool.submit(self._run, work)
        if closed:
            if reserved:
                self._release()
                _call_quietly(on_cancel)
            raise ExecutorClosed("the executor is shutting down")
        future.add_done_callback(lambda f: self._finished(f, on_cancel))
        return future

    @staticmethod
    def _run(work: Callable[[], object]) -> object:
        try:
            return work()
        except BaseException:
            # The pipeline records a Run's own failures; this is a bug outside it.
            logger.exception("Run work raised")
            raise

    def _finished(self, future: Future, on_cancel: Callable[[], object] | None) -> None:
        self._release()
        if future.cancelled():
            _call_quietly(on_cancel)

    def _release(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def close(self) -> None:
        """Refuse new work from now on (ExecutorClosed), without waiting: queued and running
        work carry on until `shutdown`. A Run created as this happens is settled by `admit`."""
        with self._lock:
            self._closed = True

    def shutdown(self) -> None:
        """Refuse new work, cancel queued work (running each `on_cancel`), and wait for the
        running work to finish."""
        self.close()
        self._pool.shutdown(wait=True, cancel_futures=True)


def _call_quietly(fn: Callable[[], object] | None) -> None:
    if fn is None:
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 - one Run's clean-up must not stop the others'
        logger.exception("cancelling Run work failed")
