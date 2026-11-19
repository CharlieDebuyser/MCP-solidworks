"""Registre des références d'entités (spec §5, §9).

Un handle opaque (`ent:face:12`) est associé à une référence persistante fournie par le backend
(octets de l'identifiant persistant SolidWorks, ou clé interne du backend simulé). La résolution
réelle se fait à l'usage, dans le backend ; un handle inconnu ou invalidé donne `ENTITY_STALE`.
"""

from __future__ import annotations

import re
from collections.abc import Hashable
from dataclasses import dataclass

from ..envelope import ErrorCode, SwError

KINDS = ("face", "edge", "sk")
_HANDLE = re.compile(r"^ent:(face|edge|sk):(\d+)$")


@dataclass(frozen=True)
class EntityRef:
    handle: str
    kind: str
    doc_id: str
    ref: Hashable


def stale(handle: str, why: str = "introuvable ou invalidé") -> SwError:
    return SwError(ErrorCode.ENTITY_STALE, f"Handle {handle[:60]!r} {why}.", details={"handle": handle[:60]})


def is_handle(value: object) -> bool:
    return isinstance(value, str) and _HANDLE.match(value) is not None


class EntityRegistry:
    def __init__(self) -> None:
        self._by_handle: dict[str, EntityRef] = {}
        self._by_key: dict[tuple[str, str, Hashable], str] = {}
        self._next = 1

    def __len__(self) -> int:
        return len(self._by_handle)

    def register(self, kind: str, doc_id: str, ref: Hashable) -> str:
        """Idempotent : la même référence dans le même document redonne le même handle."""
        if kind not in KINDS:
            raise ValueError(f"type d'entité inconnu: {kind}")
        key = (kind, doc_id, ref)
        existing = self._by_key.get(key)
        if existing is not None:
            return existing
        handle = f"ent:{kind}:{self._next}"
        self._next += 1
        self._by_handle[handle] = EntityRef(handle, kind, doc_id, ref)
        self._by_key[key] = handle
        return handle

    def resolve(self, handle: str, kind: str | None = None, doc_id: str | None = None) -> EntityRef:
        if not is_handle(handle):
            raise SwError(
                ErrorCode.INVALID_INPUT,
                f"{str(handle)[:60]!r} n'est pas un handle d'entité.",
                hint="Les handles (ent:face:n, ent:edge:n, ent:sk:n) viennent de find_faces, find_edges ou des "
                "outils d'esquisse ; ne jamais les deviner.",
            )
        entry = self._by_handle.get(handle)
        if entry is None:
            raise stale(handle)
        if kind is not None and entry.kind != kind:
            raise SwError(
                ErrorCode.INVALID_INPUT,
                f"Le handle {handle} désigne une entité de type '{entry.kind}', '{kind}' attendu.",
            )
        if doc_id is not None and entry.doc_id != doc_id:
            raise stale(handle, "appartient à un autre document")
        return entry

    def invalidate(self, handle: str) -> None:
        entry = self._by_handle.pop(handle, None)
        if entry is not None:
            self._by_key.pop((entry.kind, entry.doc_id, entry.ref), None)

    def invalidate_doc(self, doc_id: str) -> int:
        """Vide les handles d'un document (à sa fermeture)."""
        victims = [h for h, e in self._by_handle.items() if e.doc_id == doc_id]
        for h in victims:
            self.invalidate(h)
        return len(victims)

    def clear(self) -> None:
        """Instance perdue : tous les handles sont invalidés. Les numéros ne sont jamais réutilisés."""
        self._by_handle.clear()
        self._by_key.clear()
