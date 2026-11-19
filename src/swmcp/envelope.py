"""Enveloppe de réponse commune et codes d'erreur (spec §5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    NOT_CONNECTED = "NOT_CONNECTED"
    SW_DISCONNECTED = "SW_DISCONNECTED"
    SW_BUSY = "SW_BUSY"
    NO_ACTIVE_DOC = "NO_ACTIVE_DOC"
    WRONG_DOC_TYPE = "WRONG_DOC_TYPE"
    INVALID_INPUT = "INVALID_INPUT"
    ENTITY_STALE = "ENTITY_STALE"
    SELECTION_FAILED = "SELECTION_FAILED"
    SKETCH_OPEN = "SKETCH_OPEN"
    SKETCH_NOT_CLOSED = "SKETCH_NOT_CLOSED"
    FEATURE_FAILED = "FEATURE_FAILED"
    REBUILD_FAILED = "REBUILD_FAILED"
    PATH_DENIED = "PATH_DENIED"
    READ_ONLY_MODE = "READ_ONLY_MODE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    INTERNAL = "INTERNAL"


# Codes pour lesquels le même appel peut être rejoué tel quel (après l'action indiquée par `hint`).
RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.NOT_CONNECTED,
        ErrorCode.SW_DISCONNECTED,
        ErrorCode.SW_BUSY,
        ErrorCode.CONFIRMATION_REQUIRED,
    }
)

DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.NOT_CONNECTED: "Appeler sw_connect, puis rejouer l'appel.",
    ErrorCode.SW_DISCONNECTED: "SolidWorks ne répond plus : appeler sw_connect, puis relancer find_faces/find_edges "
    "(tous les handles sont invalidés).",
    ErrorCode.SW_BUSY: "SolidWorks est occupé : attendre quelques secondes et rejouer ; vérifier qu'aucun dialogue "
    "n'est ouvert.",
    ErrorCode.NO_ACTIVE_DOC: "Ouvrir un document (doc_open) ou en créer un (doc_new).",
    ErrorCode.WRONG_DOC_TYPE: "Cet outil ne s'applique pas à ce type de document : vérifier avec doc_info.",
    ErrorCode.INVALID_INPUT: "Corriger les paramètres ; les unités sont en mm et en degrés.",
    ErrorCode.ENTITY_STALE: "Le handle n'est plus valide : relancer find_faces, find_edges ou sketch_inspect.",
    ErrorCode.SELECTION_FAILED: "L'entité n'a pas pu être sélectionnée : obtenir un handle frais puis réessayer.",
    ErrorCode.SKETCH_OPEN: "Une esquisse est ouverte : appeler sketch_end avant cet outil.",
    ErrorCode.SKETCH_NOT_CLOSED: "Le contour n'est pas fermé : appeler sketch_inspect et fermer le contour.",
    ErrorCode.FEATURE_FAILED: "La fonction n'a pas été créée : lire le diagnostic joint, puis inspect_rebuild.",
    ErrorCode.REBUILD_FAILED: "Appeler inspect_rebuild pour le détail des fonctions en erreur.",
    ErrorCode.PATH_DENIED: "Utiliser un chemin situé dans un dossier autorisé (voir sw_status).",
    ErrorCode.READ_ONLY_MODE: "Modification refusée : document ou serveur en lecture seule (voir sw_status).",
    ErrorCode.CONFIRMATION_REQUIRED: "Présenter l'effet à l'utilisateur, attendre son accord, puis rappeler le même "
    "outil avec les mêmes paramètres et confirm_token.",
    ErrorCode.TIMEOUT: "Délai dépassé : vérifier les dialogues ouverts dans SolidWorks, appeler sw_status, puis "
    "contrôler l'état avant de rejouer.",
    ErrorCode.UNSUPPORTED_VERSION: "Cette fonction n'existe pas dans la version de SolidWorks connectée.",
    ErrorCode.INTERNAL: "Erreur inattendue : détail dans le journal du serveur.",
}


class SwError(Exception):
    """Erreur codée, convertie en enveloppe `ok = false`."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint if hint is not None else DEFAULT_HINTS[code]
        self.retryable = (code in RETRYABLE) if retryable is None else retryable
        self.details = details

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code.value,
            message=self.message,
            hint=self.hint,
            retryable=self.retryable,
            details=self.details,
        )


class ErrorInfo(BaseModel):
    code: str
    message: str
    hint: str
    retryable: bool
    details: dict[str, Any] | None = Field(
        default=None, description="Diagnostic joint (fonction fautive, etc.)"
    )


class DocState(BaseModel):
    """État réel relu dans SolidWorks après chaque outil, y compris en cas d'échec."""

    doc: str | None = None
    doc_type: str | None = None
    dirty: bool | None = None
    rebuild_ok: bool | None = None
    feature_count: int | None = None
    sketch_open: str | None = None


DataT = TypeVar("DataT")


class Envelope(BaseModel, Generic[DataT]):
    ok: bool
    data: DataT | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    state: DocState = Field(default_factory=DocState)
    elapsed_ms: int = 0


def invalid(message: str, hint: str | None = None, **details: Any) -> SwError:
    return SwError(ErrorCode.INVALID_INPUT, message, hint=hint, details=details or None)
