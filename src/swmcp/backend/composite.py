"""Fonctions composées d'appels élémentaires du backend, partagées par toutes les implémentations."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ..envelope import SwError
from . import geometry as g
from .models import FeatureCreated, Vec3

if TYPE_CHECKING:
    from .protocol import SwBackend


def compose_hole(
    b: SwBackend, face: str, points: list[Vec3], diameter: float, depth: float | None, name: str
) -> FeatureCreated:
    """Perçages v1 (spec §5) : esquisse de cercles sur la face, puis enlèvement de matière extrudé.

    Les points sont donnés en coordonnées du modèle et projetés sur la face grâce au repère
    renvoyé par `sketch_begin` (piège « repère de l'esquisse », spec §10).
    """
    begun = b.sketch_begin(face, f"{name}_esq")
    try:
        for p in points:
            x, y, _ = g.to_sketch(begun.frame, p)
            b.sketch_circle(x, y, diameter / 2.0)
    except SwError:
        with contextlib.suppress(SwError):
            b.sketch_end()
        raise
    b.sketch_end()
    end = "through_all" if depth is None else "blind"
    return b.extrude(begun.name, depth, end, "normal", True, 0.0, name, True)
