"""Thread COM unique et file d'appels (spec §9).

Tous les appels au backend passent par ce thread : il initialise COM en appartement mono-thread,
crée le backend (les objets COM ne quittent jamais ce thread) et exécute les commandes une à une.
Les outils (coroutines asyncio) déposent une commande et attendent un futur.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from ..envelope import ErrorCode, SwError
from .protocol import SwBackend

T = TypeVar("T")
log = logging.getLogger("swmcp.worker")

BUSY_DELAYS: tuple[float, ...] = (0.1, 0.2, 0.4, 0.8, 1.6)


def retry_rejected(
    call: Callable[[], T],
    is_rejected: Callable[[BaseException], bool],
    delays: Sequence[float] = BUSY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Rejoue `call` tant que SolidWorks l'a *refusé sans l'exécuter* (RPC_E_CALL_REJECTED...).

    Jusqu'à `len(delays)` nouveaux essais à délai croissant, puis `SW_BUSY`. Toute autre
    exception remonte telle quelle : on ne rejoue jamais un appel peut-être exécuté.
    """
    for delay in (*delays, None):
        try:
            return call()
        except BaseException as exc:
            if not is_rejected(exc):
                raise
            if delay is None:
                raise SwError(
                    ErrorCode.SW_BUSY,
                    f"SolidWorks a refusé l'appel {len(delays) + 1} fois de suite (occupé).",
                ) from exc
            sleep(delay)
    raise AssertionError("inatteignable")


@dataclass
class _Job(Generic[T]):
    fn: Callable[[SwBackend], T]
    future: Future[T] = field(default_factory=Future)
    started: threading.Event = field(default_factory=threading.Event)


class ComWorker:
    def __init__(self, backend_factory: Callable[[], SwBackend], max_queue: int = 10) -> None:
        self._factory = backend_factory
        self._queue: queue.Queue[_Job[Any] | None] = queue.Queue()
        self._max_queue = max_queue
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self.backend: SwBackend | None = None  # lecture réservée aux tests et au thread COM
        self._busy = threading.Event()

    # --- cycle de vie ------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="swmcp-com", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._init_error is not None:
            raise self._init_error

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        com: Any = None
        try:
            if sys.platform == "win32":
                try:
                    import pythoncom

                    pythoncom.CoInitialize()
                    com = pythoncom
                except ImportError:
                    com = None
            self.backend = self._factory()
        except BaseException as exc:
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                if not job.future.set_running_or_notify_cancel():
                    continue
                job.started.set()
                self._busy.set()
                try:
                    job.future.set_result(job.fn(self.backend))
                except BaseException as exc:
                    job.future.set_exception(exc)
                finally:
                    self._busy.clear()
        finally:
            with contextlib.suppress(Exception):
                self.backend.shutdown()
            self.backend = None
            if com is not None:
                com.CoUninitialize()

    # --- soumission --------------------------------------------------------------------------
    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def submit(self, fn: Callable[[SwBackend], T]) -> _Job[T]:
        if self._thread is None:
            raise SwError(ErrorCode.INTERNAL, "Le worker COM n'est pas démarré.")
        if self._queue.qsize() >= self._max_queue:
            raise SwError(
                ErrorCode.SW_BUSY,
                f"File d'appels pleine ({self._max_queue} en attente).",
                hint="Attendre la fin des appels en cours avant d'en envoyer d'autres ; un appel à la fois.",
            )
        job: _Job[T] = _Job(fn)
        self._queue.put(job)
        return job

    async def call(self, fn: Callable[[SwBackend], T], timeout: float) -> T:
        """Exécute `fn(backend)` dans le thread COM. Lève `TIMEOUT` au dépassement.

        Un appel COM bloqué ne peut pas être interrompu : au dépassement, l'appel continue
        peut-être dans SolidWorks ; l'appelant doit passer en état « suspect ».
        """
        job = self.submit(fn)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(job.future), timeout)
        except TimeoutError:
            job.future.cancel()
            raise SwError(
                ErrorCode.TIMEOUT,
                f"Délai de {timeout:g} s dépassé"
                + (
                    " : SolidWorks ne répond pas (dialogue modal ouvert ?)."
                    if job.started.is_set()
                    else " en file d'attente."
                ),
                details={"started": job.started.is_set()},
            ) from None
