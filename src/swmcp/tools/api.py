"""Familles 7 et 8 : index de l'API, fiches de connaissance, échappatoire `api_call_raw`."""

from __future__ import annotations

import fnmatch
from typing import Annotated, Any, Literal

from pydantic import Field

from .. import knowledge
from ..backend.models import RawCallResult
from ..envelope import invalid
from ..runtime import Call
from .outputs import ApiDescribeResult, ApiSearchResult, KnowledgeResult
from .registry import ConfirmToken, Cursor, tool

NOT_INDEXED = "Index de l'API absent : l'utilisateur doit lancer `swmcp index-api` sur le poste SolidWorks."


@tool("api_search", title="Chercher dans l'API SolidWorks", read_only=True)
async def api_search(
    c: Call,
    query: Annotated[
        str, Field(min_length=1, max_length=100, description="Mots-clés, ex. 'extrusion' ou 'end cond'")
    ],
    kind: Literal["interface", "method", "property", "enum", "enum_member"] | None = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
    cursor: Cursor = None,
) -> ApiSearchResult:
    """Cherche des interfaces, méthodes, propriétés et énumérations de l'API COM de SolidWorks dans l'index
    local construit depuis les bibliothèques de types du poste. Utile pour diagnostiquer ou préparer
    api_call_raw. Exemple : api_search(query="FeatureExtrusion", kind="method")."""
    index = c.rt.api_index
    if not index.exists:
        c.warn(NOT_INDEXED)
        return ApiSearchResult(indexed=False, results=[], total=0)
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        raise invalid("Curseur invalide.") from None
    hits, total = index.search(query, kind, limit, max(0, offset))
    nxt = str(offset + limit) if offset + limit < total else None
    return ApiSearchResult(indexed=True, results=hits, total=total, next_cursor=nxt)


@tool("api_describe", title="Décrire un membre de l'API", read_only=True)
async def api_describe(
    c: Call,
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=120,
            description="Ex. 'IFeatureManager.FeatureExtrusion3' ou 'swEndConditions_e'",
        ),
    ],
) -> ApiDescribeResult:
    """Signature complète d'un membre de l'API (paramètres, types, retour) ou valeurs d'une énumération, d'après
    l'index local. Exemple : api_describe(name="IModelDoc2.Extension")."""
    index = c.rt.api_index
    if not index.exists:
        c.warn(NOT_INDEXED)
        return ApiDescribeResult(indexed=False, found=False)
    doc = index.describe(name)
    if doc is not None and len(doc.members) > 60:
        c.warn(f"{doc.members_total} membres : seuls les 60 premiers sont listés ; préciser avec api_search.")
        doc.members = doc.members[:60]
    return ApiDescribeResult(indexed=True, found=doc is not None, member=doc)


@tool("knowledge_get", title="Fiche de connaissance CAO", read_only=True)
async def knowledge_get(
    c: Call,
    topic: Annotated[
        str,
        Field(
            min_length=1, max_length=60, description="Sujet, ex. 'modeling-method' ; 'index' pour la liste"
        ),
    ],
) -> KnowledgeResult:
    """Renvoie une fiche de méthode CAO (Markdown) : modeling-method, sketch-practices, features-guide,
    dfm-machining, dfm-sheet-metal, materials-and-units, error-recipes. Équivalent des ressources
    swmcp://knowledge/<sujet> pour les clients qui n'exposent que les outils. topic="index" liste les fiches."""
    sheets = knowledge.sheets()
    available = list(sheets)
    if topic == "index":
        lines = [f"- **{s.topic}** — {s.title} : {s.when_to_use}" for s in sheets.values()]
        return KnowledgeResult(
            topic="index",
            title="Fiches disponibles",
            version=None,
            content="\n".join(lines),
            available=available,
        )
    sheet = sheets.get(topic)
    if sheet is None:
        raise invalid(f"Fiche inconnue : {topic[:60]}", hint="Fiches disponibles : " + ", ".join(available))
    return KnowledgeResult(
        topic=topic, title=sheet.title, version=sheet.version, content=sheet.body, available=available
    )


Scalar = str | int | float | bool | None


@tool("api_call_raw", title="Appel COM brut", profile="full", destructive=True, flag="raw_api")
async def api_call_raw(
    c: Call,
    target: Annotated[
        Literal["app", "doc", "ext"],
        Field(description="app : ISldWorks ; doc : document actif ; ext : son Extension"),
    ],
    method: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")],
    args: Annotated[
        list[Scalar],
        Field(max_length=16, description="Arguments scalaires, en unités de l'API (mètres, radians)"),
    ] = [],  # noqa: B006
    confirm_token: ConfirmToken = None,
) -> RawCallResult:
    """Échappatoire : appelle une méthode COM de la liste blanche sur l'application, le document actif ou son
    extension. Unités de l'API : mètres et radians. Confirmation systématique de l'utilisateur, appel journalisé.
    À n'utiliser que si aucun outil du catalogue ne convient ; lire d'abord api_describe."""
    key = f"{target}.{method}"
    if not any(fnmatch.fnmatchcase(key, pattern) for pattern in c.settings.raw_api_allow):
        raise invalid(
            f"{key} n'est pas dans la liste blanche de api_call_raw.",
            hint="Seul l'utilisateur peut l'étendre (raw_api_allow dans la configuration). Membres autorisés : "
            + ", ".join(c.settings.raw_api_allow[:12]),
        )
    await c.confirm(f"Appel COM brut {key}({', '.join(repr(a) for a in args)}) sur SolidWorks.")
    result: Any = await c.sw(lambda b: b.raw_call(target, method, list(args)))
    if not isinstance(result, str | int | float | bool | type(None)):
        result = str(result)[:500]
    return RawCallResult(target=target, method=method, result=result)
