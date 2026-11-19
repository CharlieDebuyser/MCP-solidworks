"""Runtime du serveur : configuration, worker, garde-fous et exécution d'un appel d'outil.

Le serveur ne garde que le registre de handles (dans le backend), les jetons de confirmation et
la file d'appels ; SolidWorks reste la source de vérité (spec §3).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from .apiindex import ApiIndex
from .backend.protocol import SwBackend
from .backend.worker import ComWorker
from .config import Settings
from .envelope import DocState, ErrorCode, SwError
from .safety.audit import AuditLog
from .safety.checkpoints import CheckpointManager
from .safety.confirm import ConfirmStore
from .safety.paths import PathGuard

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context

    from .tools.registry import ToolSpec

T = TypeVar("T")
log = logging.getLogger("swmcp.runtime")


def default_backend_factory(settings: Settings) -> Callable[[], SwBackend]:
    if settings.effective_backend == "fake":
        from .backend.fake_backend import FakeSwBackend

        return lambda: FakeSwBackend()

    def make() -> SwBackend:
        from .backend.com_backend import ComSwBackend

        backend: SwBackend = ComSwBackend()
        return backend

    return make


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: deque[float] = deque()

    def hit(self) -> None:
        now = self._clock()
        while self._hits and now - self._hits[0] >= 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.per_minute:
            raise SwError(
                ErrorCode.SW_BUSY,
                f"Plafond de {self.per_minute} appels par minute atteint.",
                hint="Ralentir : attendre quelques secondes avant le prochain appel.",
            )
        self._hits.append(now)


class Runtime:
    def __init__(self, settings: Settings, backend_factory: Callable[[], SwBackend] | None = None) -> None:
        self.settings = settings
        self.worker = ComWorker(backend_factory or default_backend_factory(settings), settings.max_queue)
        self.paths = PathGuard(settings.allowed_dirs, settings.workspace)
        self.confirm = ConfirmStore(settings.confirm_ttl_s)
        self.audit = AuditLog(settings.log_dir)
        self.checkpoints = CheckpointManager(
            settings.workspace, settings.checkpoint_max_per_doc, settings.checkpoint_max_total_mb
        )
        self.limiter = RateLimiter(settings.max_calls_per_minute)
        self.api_index = ApiIndex(settings.api_index_path)
        self.suspect = False
        self.was_connected = False
        self._checkpointed: set[str] = set()
        self._started = False

    def start(self) -> None:
        if not self._started:
            with contextlib.suppress(OSError):
                self.settings.workspace.mkdir(parents=True, exist_ok=True)
            self.worker.start()
            self._started = True

    def stop(self) -> None:
        if self._started:
            self.worker.stop()
            self.audit.close()
            self._started = False

    # --- exécution dans le thread COM ---------------------------------------------------------
    def _ensure_connected(self, b: SwBackend) -> None:
        if b.is_connected():
            return
        if self.was_connected and self.settings.auto_reconnect:
            log.info("reconnexion automatique à SolidWorks")
            b.connect(self.settings.launch_sw, None)
            return
        raise SwError(ErrorCode.NOT_CONNECTED, "Aucune connexion à SolidWorks.")

    def _auto_checkpoint(self, b: SwBackend, warnings: list[str]) -> None:
        """Point de restauration avant la première modification d'un document enregistré (spec §8)."""
        info = b.doc_info()
        if info.read_only:
            raise SwError(
                ErrorCode.READ_ONLY_MODE,
                f"Le document {info.name} est ouvert en lecture seule.",
                hint="Le rouvrir avec doc_open(read_only=false), dans un dossier autorisé, avant de le modifier.",
            )
        if info.path is None or info.path in self._checkpointed:
            return
        self._checkpointed.add(info.path)
        try:
            cp = self.checkpoints.create(info.name, "auto", b.save_copy)
            warnings.append(f"Point de restauration automatique créé : {cp.id}")
        except (SwError, OSError) as exc:
            warnings.append(f"Point de restauration automatique impossible : {exc}")

    async def run(
        self,
        fn: Callable[[SwBackend], T],
        *,
        timeout: float,
        connect: bool = True,
        modifies: bool = False,
        expected_doc: str | None = None,
        warnings: list[str] | None = None,
    ) -> tuple[T | None, SwError | None, DocState]:
        """Exécute `fn` dans le worker et rend (résultat, erreur, état réel relu ensuite)."""
        notes = warnings if warnings is not None else []

        def job(b: SwBackend) -> tuple[T | None, SwError | None, DocState]:
            result: T | None = None
            error: SwError | None = None
            try:
                if connect:
                    self._ensure_connected(b)
                if modifies or expected_doc is not None:
                    active = b.state().doc
                    if expected_doc is not None and active != expected_doc:
                        raise SwError(
                            ErrorCode.INVALID_INPUT,
                            f"Le document actif est {active!r}, pas {expected_doc!r} : aucune modification faite.",
                            hint="L'utilisateur a peut-être changé de fenêtre : appeler doc_activate, ou corriger "
                            "expected_doc.",
                            details={"active_doc": active},
                        )
                    if modifies and active is not None:
                        self._auto_checkpoint(b, notes)
                result = fn(b)
                if b.is_connected():
                    self.was_connected = True
            except SwError as exc:
                error = exc
            except Exception as exc:
                log.exception("erreur inattendue dans le worker")
                error = SwError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}"[:300])
            if error is not None and error.code == ErrorCode.SW_DISCONNECTED:
                with contextlib.suppress(Exception):
                    b.mark_disconnected()
                self._checkpointed.clear()
            # retour à un état propre, puis relecture de l'état réel (spec §9)
            state = DocState()
            if b.is_connected():
                with contextlib.suppress(Exception):
                    b.cleanup()
                with contextlib.suppress(Exception):
                    state = b.state()
            return result, error, state

        try:
            return await self.worker.call(job, timeout)
        except SwError as exc:
            if exc.code == ErrorCode.TIMEOUT and (exc.details or {}).get("started"):
                self.suspect = True
            return None, exc, DocState()

    async def probe(self, timeout: float = 5.0) -> bool:
        """Vérifie que SolidWorks répond ; lève l'état « suspect » si c'est le cas."""
        if self.worker.busy:
            return False
        _, err, _ = await self.run(lambda b: b.ping(), timeout=timeout, connect=False)
        if err is None:
            self.suspect = False
        return err is None


class Call:
    """Contexte d'un appel d'outil : accès au backend, avertissements, confirmation."""

    def __init__(
        self, rt: Runtime, spec: ToolSpec, params: dict[str, Any], ctx: Context[Any, Any, Any] | None
    ) -> None:
        self.rt = rt
        self.spec = spec
        self.params = params
        self.ctx = ctx
        self.warnings: list[str] = []
        self.state: DocState | None = None
        self.images: list[bytes] = []

    @property
    def settings(self) -> Settings:
        return self.rt.settings

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    async def sw(
        self, fn: Callable[[SwBackend], T], *, connect: bool = True, modifies: bool | None = None
    ) -> T:
        """Exécute `fn(backend)` dans le thread COM avec les garde-fous de l'outil."""
        timeout = self.settings.long_timeout_s if self.spec.long else self.settings.timeout_s
        result, error, state = await self.rt.run(
            fn,
            timeout=timeout,
            connect=connect,
            modifies=self.spec.modifies if modifies is None else modifies,
            expected_doc=self.params.get("expected_doc"),
            warnings=self.warnings,
        )
        if error is None or error.code != ErrorCode.TIMEOUT:
            self.state = state
        if error is not None:
            raise error
        return result  # type: ignore[return-value]

    async def confirm(self, effect: str) -> None:
        """Confirmation en deux temps ; élicitation MCP si le client la prend en charge (spec §8)."""
        token = self.params.get("confirm_token")
        if not token and self.ctx is not None and await self._elicit(effect):
            return
        self.rt.confirm.require(self.spec.name, self.params, token, effect)

    async def _elicit(self, effect: str) -> bool:
        from pydantic import BaseModel, Field

        class Confirmation(BaseModel):
            confirmer: bool = Field(description="Cocher pour autoriser l'action")

        assert self.ctx is not None
        try:
            caps = self.ctx.session.client_params.capabilities
            if getattr(caps, "elicitation", None) is None:
                return False
            answer = await self.ctx.elicit(message=f"SolidWorks — {effect} Confirmer ?", schema=Confirmation)
        except Exception:
            return False
        if answer.action == "accept" and answer.data.confirmer:
            return True
        raise SwError(
            ErrorCode.CONFIRMATION_REQUIRED,
            "L'utilisateur a refusé l'action.",
            hint="Ne pas rejouer sans nouvelle demande explicite de l'utilisateur.",
            retryable=False,
        )

    async def progress(self, done: float, total: float, message: str | None = None) -> None:
        if self.ctx is not None:
            with contextlib.suppress(Exception):
                await self.ctx.report_progress(done, total, message)
