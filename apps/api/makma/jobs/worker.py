"""Worker loop.

Runs either as a thread inside the API (the zero-infrastructure default) or as
a separate process (`python -m makma.jobs.worker`) once GPU and CPU work need
to scale apart.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time

from ..config import settings
from . import handlers  # noqa: F401  (registers the handlers)
from .queue import claim_next, requeue_stalled, run_job

log = logging.getLogger("makma.jobs.worker")

_stop = threading.Event()
_threads: list[threading.Thread] = []


def worker_name(index: int = 0) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{index}"


def _loop(index: int, idle_sleep: float = 0.5) -> None:
    name = worker_name(index)
    log.info("worker %s started", name)
    last_sweep = 0.0
    while not _stop.is_set():
        try:
            if time.time() - last_sweep > 120:
                recovered = requeue_stalled()
                if recovered:
                    log.warning("recovered %d stalled job(s)", recovered)
                last_sweep = time.time()

            job = claim_next(name)
            if job is None:
                _stop.wait(idle_sleep)
                continue
            log.info("worker %s running %s job %s", name, job.type, job.id)
            run_job(job)
        except Exception:  # noqa: BLE001 - the loop must outlive any single failure
            log.exception("worker loop error")
            _stop.wait(2.0)
    log.info("worker %s stopped", name)


def start_inline_workers(count: int | None = None) -> None:
    """Start background worker threads inside the API process."""
    if _threads:
        return
    count = count or settings.worker_concurrency
    for i in range(max(1, count)):
        thread = threading.Thread(target=_loop, args=(i,), daemon=True,
                                  name=f"makma-worker-{i}")
        thread.start()
        _threads.append(thread)


def stop_inline_workers(timeout: float = 5.0) -> None:
    _stop.set()
    for thread in _threads:
        thread.join(timeout=timeout)
    _threads.clear()
    _stop.clear()


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    from ..db import init_db

    init_db()

    def _handle_signal(_sig, _frame):
        log.info("shutting down")
        _stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    count = max(1, settings.worker_concurrency)
    threads = [
        threading.Thread(target=_loop, args=(i,), name=f"worker-{i}")
        for i in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":  # pragma: no cover
    main()
