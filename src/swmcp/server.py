"""Application FastMCP : enregistrement des outils, ressources et prompts (spec §3, §4).

Rien n'est écrit sur stdout en dehors du protocole MCP.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock
from pydantic import ValidationError

from . import __version__, knowledge, prompts
from .backend.protocol import SwBackend
from .config import Settings
from .envelope import ErrorCode, SwError
from .runtime import Runtime
from .safety.audit import setup_logging
from .tools import exposed
from .tools.registry import build_handler, error_envelope, to_result

log = logging.getLogger("swmcp.server")


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(x) for x in err["loc"]) or "paramètres"
        parts.append(f"{loc}: {err['msg']}")
    return "Paramètres invalides — " + " ; ".join(parts)


def _reader(body: str) -> Callable[[], str]:
    def read() -> str:
        return body

    return read


class SwFastMCP(FastMCP[Any]):
    """FastMCP dont les erreurs de validation deviennent une enveloppe INVALID_INPUT (spec §5)."""

    runtime: Runtime

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                error = SwError(ErrorCode.INVALID_INPUT, _validation_message(exc.__cause__))
                self.runtime.audit.record(
                    tool=name,
                    params=arguments,
                    code=error.code.value,
                    elapsed_ms=0,
                    doc=None,
                    request_id=None,
                )
                return to_result(error_envelope(error))  # type: ignore[return-value]
            raise


def create_server(
    settings: Settings, backend_factory: Callable[[], SwBackend] | None = None
) -> tuple[SwFastMCP, Runtime]:
    rt = Runtime(settings, backend_factory)

    @asynccontextmanager
    async def lifespan(_: FastMCP[Any]) -> AsyncIterator[None]:
        rt.start()
        try:
            yield
        finally:
            rt.stop()

    mcp = SwFastMCP(
        name="solidworks",
        instructions=knowledge.instructions(),
        lifespan=lifespan,
        log_level=settings.log_level,
    )
    mcp.runtime = rt
    mcp._mcp_server.version = __version__

    for spec in exposed(settings):
        mcp.add_tool(
            build_handler(rt, spec),
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=spec.annotations,
            structured_output=True,
        )

    for sheet in knowledge.sheets().values():
        mcp.resource(
            sheet.uri,
            name=sheet.topic,
            title=sheet.title,
            description=sheet.when_to_use,
            mime_type="text/markdown",
        )(_reader(sheet.body))

    prompts.register(mcp, rt)
    log.info(
        "serveur prêt",
        extra={
            "swmcp": {
                "profile": settings.profile,
                "backend": settings.effective_backend,
                "tools": len(exposed(settings)),
            }
        },
    )
    return mcp, rt


def serve(settings: Settings) -> None:
    """Point d'entrée de `swmcp serve` : transport stdio (spec §14)."""
    setup_logging(settings.log_dir, settings.log_level)
    mcp, _ = create_server(settings)
    mcp.run("stdio")
