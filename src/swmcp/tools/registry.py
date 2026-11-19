"""Registre des outils MCP et exécution commune (spec §3 « Déroulé d'un appel d'outil »).

Un outil est une coroutine `async def outil(c: Call, ...) -> Modèle` : elle valide, appelle le
backend par `c.sw(...)` et renvoie des données. Ce module fournit le reste : profil d'exposition,
plafond d'appels, état « suspect », enveloppe, journal d'audit, sortie structurée.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.fastmcp import Context
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from ..envelope import DocState, Envelope, ErrorCode, SwError
from ..runtime import Call, Runtime

log = logging.getLogger("swmcp.tools")
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# --- types de paramètres partagés ---------------------------------------------------------------
Name = Annotated[
    str | None,
    Field(
        description="Nom stable donné à l'objet créé (lettres, chiffres, espace, _ . -). Généré (<Type>_<n>) si omis.",
        max_length=40,
        pattern=r"^[A-Za-z0-9À-ÿ_][A-Za-z0-9À-ÿ_ .\-]*$",
    ),
]
ExpectedDoc = Annotated[
    str | None,
    Field(description="Nom du document qui doit être actif ; l'outil refuse d'agir si c'en est un autre."),
]
ConfirmToken = Annotated[
    str | None,
    Field(description="Jeton reçu avec CONFIRMATION_REQUIRED, à renvoyer après accord de l'utilisateur."),
]
Verify = Annotated[
    Literal["basic", "full"],
    Field(
        description="Niveau de preuve renvoyé : basic (volume, corps, reconstruction) ou full (+ boîte, faces, arêtes)."
    ),
]
Handle = Annotated[
    str, Field(pattern=r"^ent:(face|edge|sk):\d+$", description="Handle d'entité, ex. ent:face:12")
]
Mm = Annotated[float, Field(gt=0, le=100_000)]
Coord = Annotated[float, Field(ge=-100_000, le=100_000)]
Limit = Annotated[int, Field(ge=1, le=50, description="Nombre maximal d'éléments renvoyés")]
Cursor = Annotated[str | None, Field(description="Curseur `next_cursor` de la page précédente")]


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Awaitable[Any]]
    title: str
    profile: str
    read_only: bool
    destructive: bool
    idempotent: bool
    modifies: bool
    long: bool
    flag: str | None
    description: str
    data_model: Any
    params: list[inspect.Parameter]

    @property
    def annotations(self) -> ToolAnnotations:
        return ToolAnnotations(
            title=self.title,
            readOnlyHint=self.read_only,
            destructiveHint=self.destructive,
            idempotentHint=self.idempotent,
            openWorldHint=False,
        )


TOOLS: dict[str, ToolSpec] = {}


def tool(
    name: str,
    *,
    title: str,
    profile: Literal["inspect", "design", "full"] = "inspect",
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    modifies: bool = False,
    long: bool = False,
    flag: str | None = None,
) -> Callable[[F], F]:
    """Enregistre un outil. `modifies` : modifie le document actif (contrôles `expected_doc`,
    lecture seule et point de restauration automatique)."""

    def deco(fn: F) -> F:
        hints = typing.get_type_hints(fn, include_extras=True)
        sig = inspect.signature(fn)
        params = [p.replace(annotation=hints[p.name]) for p in list(sig.parameters.values())[1:]]
        doc = inspect.getdoc(fn)
        if not doc:
            raise ValueError(f"l'outil {name} n'a pas de description")
        if name in TOOLS:
            raise ValueError(f"outil en double : {name}")
        TOOLS[name] = ToolSpec(
            name=name,
            fn=fn,
            title=title,
            profile=profile,
            read_only=read_only,
            destructive=destructive,
            idempotent=idempotent or read_only,
            modifies=modifies,
            long=long,
            flag=flag,
            description=doc,
            data_model=hints["return"],
            params=params,
        )
        return fn

    return deco


def to_result(envelope: Envelope[Any], images: list[bytes] | None = None) -> CallToolResult:
    """Sortie structurée + texte JSON équivalent (spec §5, convention 7)."""
    payload = envelope.model_dump(mode="json")
    content: list[Any] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    for png in images or []:
        content.append(
            ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png")
        )
    is_error = not envelope.ok and (
        envelope.error is None or envelope.error.code != ErrorCode.CONFIRMATION_REQUIRED
    )
    return CallToolResult(content=content, structuredContent=payload, isError=is_error)


def error_envelope(error: SwError, state: DocState | None = None, elapsed_ms: int = 0) -> Envelope[Any]:
    return Envelope[Any](ok=False, error=error.to_info(), state=state or DocState(), elapsed_ms=elapsed_ms)


async def execute(
    rt: Runtime, spec: ToolSpec, params: dict[str, Any], ctx: Context[Any, Any, Any] | None
) -> CallToolResult:
    t0 = time.perf_counter()
    call = Call(rt, spec, params, ctx)
    data: Any = None
    error: SwError | None = None
    try:
        rt.limiter.hit()
        if not rt.settings.allows(spec.profile):
            raise SwError(
                ErrorCode.READ_ONLY_MODE,
                f"L'outil {spec.name} n'est pas autorisé par le profil '{rt.settings.profile}'.",
            )
        if rt.suspect and not spec.read_only and spec.name != "sw_connect":
            raise SwError(
                ErrorCode.TIMEOUT,
                "Un appel précédent a dépassé son délai : les outils modifiants sont suspendus.",
                hint="Vérifier les dialogues ouverts dans SolidWorks, puis appeler sw_status pour confirmer qu'il "
                "répond ; contrôler ensuite l'état du modèle avant de rejouer.",
            )
        data = await spec.fn(call, **params)
    except SwError as exc:
        error = exc
    except Exception as exc:
        log.exception("erreur inattendue dans l'outil %s", spec.name)
        error = SwError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}"[:300])
    state = call.state
    if state is None:
        state = DocState()
        if not rt.worker.busy and not rt.suspect:
            _, _, state = await rt.run(lambda b: None, timeout=5.0, connect=False)
    elapsed = int((time.perf_counter() - t0) * 1000)
    envelope: Envelope[Any]
    if error is None:
        envelope = Envelope[Any](ok=True, data=data, warnings=call.warnings, state=state, elapsed_ms=elapsed)
    else:
        envelope = Envelope[Any](
            ok=False, warnings=call.warnings, error=error.to_info(), state=state, elapsed_ms=elapsed
        )
    request_id = None
    if ctx is not None:
        try:
            request_id = str(ctx.request_id)
        except Exception:
            request_id = None
    rt.audit.record(
        tool=spec.name,
        params=params,
        code="OK" if error is None else error.code.value,
        elapsed_ms=elapsed,
        doc=state.doc,
        request_id=request_id,
    )
    return to_result(envelope, call.images)


def build_handler(rt: Runtime, spec: ToolSpec) -> Callable[..., Awaitable[CallToolResult]]:
    """Fabrique la fonction enregistrée auprès de FastMCP, avec la signature typée de l'outil."""

    async def handler(**kwargs: Any) -> CallToolResult:
        ctx = kwargs.pop("ctx", None)
        return await execute(rt, spec, kwargs, ctx)

    ret = Annotated[CallToolResult, Envelope[spec.data_model]]  # type: ignore[name-defined]
    ctx_param = inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)
    kw = [p.replace(kind=inspect.Parameter.KEYWORD_ONLY) for p in spec.params]
    handler.__signature__ = inspect.Signature([*kw, ctx_param], return_annotation=ret)  # type: ignore[attr-defined]
    handler.__annotations__ = {**{p.name: p.annotation for p in spec.params}, "ctx": Context, "return": ret}
    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    return handler


def exposed(settings: Any) -> list[ToolSpec]:
    """Outils exposés par le profil : les autres sont retirés de la liste, pas refusés à l'appel (spec §8)."""
    out = []
    for spec in TOOLS.values():
        if not settings.allows(spec.profile):
            continue
        if spec.flag and not getattr(settings, spec.flag):
            continue
        out.append(spec)
    return out


# --- aides communes aux outils ----------------------------------------------------------------

ItemT = TypeVar("ItemT")


class Page(BaseModel):
    total: int
    next_cursor: str | None = None


def paginate(items: list[ItemT], limit: int, cursor: str | None) -> tuple[list[ItemT], int, str | None]:
    try:
        start = int(cursor) if cursor else 0
    except ValueError:
        raise SwError(
            ErrorCode.INVALID_INPUT, f"Curseur invalide : {cursor[:40] if cursor else cursor}"
        ) from None
    if start < 0 or start > len(items):
        raise SwError(ErrorCode.INVALID_INPUT, "Curseur hors limites.")
    end = start + limit
    return items[start:end], len(items), (str(end) if end < len(items) else None)
