"""Famille 3 : esquisse. Coordonnées en mm dans le repère de l'esquisse renvoyé par sketch_begin."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from ..backend.models import (
    DimensionResult,
    DimensionType,
    RelationResult,
    RelationType,
    SketchBeginResult,
    SketchEndResult,
    SketchEntityRef,
    SketchInspectResult,
)
from ..backend.protocol import SwBackend
from ..envelope import invalid
from ..runtime import Call
from .outputs import SketchEntities
from .registry import Coord, ExpectedDoc, Mm, Name, tool

SketchEntity = Annotated[
    str,
    Field(
        pattern=r"^(origin|ent:sk:\d+)$",
        description="Handle ent:sk:n d'un segment ou d'un point, ou 'origin'",
    ),
]
PlaneRef = Annotated[
    str,
    Field(
        pattern=r"^(front|top|right|ent:face:\d+)$",
        description="'front' (XY), 'top' (XZ), 'right' (YZ) ou handle d'une face plane (find_faces)",
    ),
]
EPS = 1e-6


async def _add(c: Call, fn: Callable[[SwBackend], list[SketchEntityRef]]) -> SketchEntities:
    entities = await c.sw(fn)
    return SketchEntities(sketch=c.state.sketch_open if c.state else None, entities=entities)


@tool("sketch_begin", title="Ouvrir une esquisse", profile="design", modifies=True)
async def sketch_begin(
    c: Call, plane: PlaneRef, name: Name = None, expected_doc: ExpectedDoc = None
) -> SketchBeginResult:
    """Ouvre une esquisse sur un plan de référence ou une face plane. Renvoie le repère de l'esquisse (origine,
    axes X et Y, normale) dans le repère du modèle : s'y fier pour placer les entités, ne jamais le supposer.
    Une seule esquisse ouverte à la fois ; la fermer par sketch_end. Exemple : sketch_begin(plane="front", name="Profil")."""
    return await c.sw(lambda b: b.sketch_begin(plane, name))


@tool("sketch_end", title="Fermer l'esquisse", profile="design", modifies=True)
async def sketch_end(c: Call, expected_doc: ExpectedDoc = None) -> SketchEndResult:
    """Quitte l'esquisse ouverte et contrôle son état : statut de contrainte (under_defined, fully_defined,
    over_defined, failed) et contour fermé ou non. Viser fully_defined et closed=true avant une fonction."""
    result = await c.sw(lambda b: b.sketch_end())
    if result.status == "under_defined":
        c.warn(
            "Esquisse sous-contrainte : ajouter relations et cotes (sketch_add_relation, sketch_add_dimension)."
        )
    if not result.closed and result.segment_count:
        c.warn("Contour non exploitable pour une fonction : " + "; ".join(result.issues[:3]))
    return result


@tool("sketch_add_line", title="Ligne d'esquisse", profile="design", modifies=True)
async def sketch_add_line(
    c: Call,
    x1_mm: Coord,
    y1_mm: Coord,
    x2_mm: Coord,
    y2_mm: Coord,
    construction: Annotated[
        bool, Field(description="Ligne de construction (axe de révolution, symétrie)")
    ] = False,
    expected_doc: ExpectedDoc = None,
) -> SketchEntities:
    """Ajoute une ligne dans l'esquisse ouverte (mm). Renvoie les handles de la ligne et de ses deux points.
    Exemple : sketch_add_line(x1_mm=0, y1_mm=0, x2_mm=60, y2_mm=0)."""
    if math.hypot(x2_mm - x1_mm, y2_mm - y1_mm) < EPS:
        raise invalid("Ligne de longueur nulle.")
    return await _add(c, lambda b: b.sketch_line(x1_mm, y1_mm, x2_mm, y2_mm, construction))


@tool("sketch_add_rect", title="Rectangle d'esquisse", profile="design", modifies=True)
async def sketch_add_rect(
    c: Call,
    x1_mm: Coord,
    y1_mm: Coord,
    x2_mm: Coord,
    y2_mm: Coord,
    mode: Annotated[
        Literal["corner", "center"],
        Field(
            description="corner : (x1,y1) et (x2,y2) sont deux coins opposés ; center : (x1,y1) est le centre, (x2,y2) un coin"
        ),
    ] = "corner",
    expected_doc: ExpectedDoc = None,
) -> SketchEntities:
    """Ajoute un rectangle aligné sur les axes de l'esquisse (mm), avec ses relations horizontales et verticales.
    Renvoie 4 lignes (bottom, right, top, left) et 4 points. Exemple : sketch_add_rect(x1_mm=0, y1_mm=0, x2_mm=60, y2_mm=40)."""
    if mode == "center":
        x1_mm, y1_mm = 2 * x1_mm - x2_mm, 2 * y1_mm - y2_mm
    if abs(x2_mm - x1_mm) < EPS or abs(y2_mm - y1_mm) < EPS:
        raise invalid("Rectangle dégénéré : largeur ou hauteur nulle.")
    return await _add(c, lambda b: b.sketch_rect(x1_mm, y1_mm, x2_mm, y2_mm))


@tool("sketch_add_circle", title="Cercle d'esquisse", profile="design", modifies=True)
async def sketch_add_circle(
    c: Call,
    cx_mm: Coord,
    cy_mm: Coord,
    radius_mm: Mm | None = None,
    diameter_mm: Mm | None = None,
    expected_doc: ExpectedDoc = None,
) -> SketchEntities:
    """Ajoute un cercle (mm) ; donner radius_mm OU diameter_mm. Renvoie les handles du cercle et de son centre.
    Exemple : sketch_add_circle(cx_mm=10, cy_mm=10, diameter_mm=5)."""
    if (radius_mm is None) == (diameter_mm is None):
        raise invalid("Donner exactement un des deux paramètres : radius_mm ou diameter_mm.")
    r = radius_mm if radius_mm is not None else diameter_mm / 2  # type: ignore[operator]
    return await _add(c, lambda b: b.sketch_circle(cx_mm, cy_mm, r))


@tool("sketch_add_arc", title="Arc d'esquisse", profile="design", modifies=True)
async def sketch_add_arc(
    c: Call,
    cx_mm: Coord,
    cy_mm: Coord,
    x1_mm: Coord,
    y1_mm: Coord,
    x2_mm: Coord,
    y2_mm: Coord,
    direction: Annotated[
        Literal["ccw", "cw"], Field(description="Sens de parcours du début vers la fin")
    ] = "ccw",
    expected_doc: ExpectedDoc = None,
) -> SketchEntities:
    """Ajoute un arc de centre (cx,cy) allant de (x1,y1) à (x2,y2), en mm. Les deux extrémités doivent être à la
    même distance du centre. Exemple : sketch_add_arc(cx_mm=0, cy_mm=0, x1_mm=10, y1_mm=0, x2_mm=0, y2_mm=10)."""
    r1, r2 = math.hypot(x1_mm - cx_mm, y1_mm - cy_mm), math.hypot(x2_mm - cx_mm, y2_mm - cy_mm)
    if r1 < EPS or abs(r1 - r2) > 1e-3:
        raise invalid(
            f"Extrémités à des distances différentes du centre ({r1:.4f} et {r2:.4f} mm).",
            hint="Recalculer les extrémités pour qu'elles soient sur le même cercle.",
        )
    if math.hypot(x2_mm - x1_mm, y2_mm - y1_mm) < EPS:
        raise invalid("Extrémités confondues : utiliser sketch_add_circle pour un cercle complet.")
    return await _add(c, lambda b: b.sketch_arc(cx_mm, cy_mm, x1_mm, y1_mm, x2_mm, y2_mm, direction == "ccw"))


@tool("sketch_add_polyline", title="Polyligne d'esquisse", profile="design", modifies=True)
async def sketch_add_polyline(
    c: Call,
    points_mm: Annotated[
        list[tuple[Coord, Coord]], Field(min_length=2, max_length=200, description="Sommets [x, y] en mm")
    ],
    closed: Annotated[bool, Field(description="Relier le dernier sommet au premier")] = False,
    expected_doc: ExpectedDoc = None,
) -> SketchEntities:
    """Ajoute une suite de lignes jointives (mm) ; closed=true ferme le contour. Ne pas répéter le premier point à
    la fin. Aucune relation n'est posée : ajouter horizontal/vertical ensuite.
    Exemple (profil en L) : points_mm=[[0,0],[60,0],[60,3],[3,3],[3,40],[0,40]], closed=true."""
    pts = [(float(x), float(y)) for x, y in points_mm]
    n = len(pts)
    if closed and n < 3:
        raise invalid("Un contour fermé exige au moins 3 sommets.")
    for i in range(n if closed else n - 1):
        if math.dist(pts[i], pts[(i + 1) % n]) < EPS:
            raise invalid(
                f"Sommets {i + 1} et {(i + 1) % n + 1} confondus.",
                hint="Ne pas répéter le premier point à la fin.",
            )
    return await _add(c, lambda b: b.sketch_polyline(pts, closed))


@tool("sketch_add_slot", title="Fente d'esquisse", profile="design", modifies=True)
async def sketch_add_slot(
    c: Call,
    x1_mm: Coord,
    y1_mm: Coord,
    x2_mm: Coord,
    y2_mm: Coord,
    width_mm: Mm,
    expected_doc: ExpectedDoc = None,
) -> SketchEntities:
    """Ajoute une fente droite (oblong) : (x1,y1) et (x2,y2) sont les centres des deux demi-cercles, width_mm la
    largeur. Exemple : sketch_add_slot(x1_mm=10, y1_mm=10, x2_mm=30, y2_mm=10, width_mm=6)."""
    if math.hypot(x2_mm - x1_mm, y2_mm - y1_mm) < EPS:
        raise invalid("Les deux centres de la fente sont confondus : utiliser sketch_add_circle.")
    return await _add(c, lambda b: b.sketch_slot(x1_mm, y1_mm, x2_mm, y2_mm, width_mm))


@tool("sketch_add_point", title="Point d'esquisse", profile="design", modifies=True)
async def sketch_add_point(
    c: Call, x_mm: Coord, y_mm: Coord, expected_doc: ExpectedDoc = None
) -> SketchEntities:
    """Ajoute un point d'esquisse (mm), utile comme repère de cote ou de perçage."""
    return await _add(c, lambda b: b.sketch_point(x_mm, y_mm))


@tool("sketch_add_relation", title="Relation d'esquisse", profile="design", modifies=True)
async def sketch_add_relation(
    c: Call,
    type: RelationType,
    entities: Annotated[list[SketchEntity], Field(min_length=1, max_length=20)],
    expected_doc: ExpectedDoc = None,
) -> RelationResult:
    """Ajoute une relation géométrique entre entités de l'esquisse ouverte. Poser les relations avant les cotes.
    horizontal/vertical : une ou plusieurs lignes, ou deux points ; coincident : un point + un point, un segment
    ou 'origin' ; symmetric : deux entités + la ligne d'axe. Renvoie le statut de contrainte.
    Exemple : sketch_add_relation(type="coincident", entities=["ent:sk:5", "origin"])."""
    if "origin" in entities and type not in ("coincident", "fix", "horizontal", "vertical", "midpoint"):
        raise invalid(f"'origin' n'est pas accepté par la relation {type}.")
    if len(set(entities)) != len(entities):
        raise invalid("La même entité est citée deux fois.")
    return await c.sw(lambda b: b.sketch_relation(type, entities))


@tool("sketch_add_dimension", title="Cote d'esquisse", profile="design", modifies=True)
async def sketch_add_dimension(
    c: Call,
    type: DimensionType,
    entities: Annotated[list[SketchEntity], Field(min_length=1, max_length=2)],
    value: Annotated[float, Field(gt=0, le=100_000, description="mm, ou degrés pour type=angle")],
    name: Name = None,
    expected_doc: ExpectedDoc = None,
) -> DimensionResult:
    """Ajoute une cote pilotante à l'esquisse ouverte : length (1 ligne), diameter ou radius (1 cercle/arc),
    distance (2 entités, 'origin' accepté en premier), angle (2 lignes, en degrés). Donner un `name` aux cotes
    qui portent une intention : le nom complet renvoyé (ex. Epaisseur@Profil) sert ensuite à dimension_set.
    Exemple : sketch_add_dimension(type="length", entities=["ent:sk:3"], value=60, name="Longueur")."""
    if type == "angle" and value >= 360:
        raise invalid("Un angle doit être inférieur à 360 degrés.")
    return await c.sw(lambda b: b.sketch_dimension(type, entities, value, name))


@tool("sketch_inspect", title="Inspecter une esquisse", read_only=True)
async def sketch_inspect(
    c: Call,
    name: Annotated[
        str | None, Field(description="Nom de l'esquisse ; défaut : l'esquisse ouverte", max_length=80)
    ] = None,
) -> SketchInspectResult:
    """Détail d'une esquisse : segments (handles, coordonnées en mm), relations, cotes, statut de contrainte,
    contour fermé ou non et problèmes détectés (extrémités libres, segments sécants ou dupliqués)."""
    result = await c.sw(lambda b: b.sketch_inspect(name))
    if len(result.segments) > 60:
        c.warn(f"Esquisse de {len(result.segments)} segments : seuls les 60 premiers sont renvoyés.")
        result.segments = result.segments[:60]
    result.relations = result.relations[:80]
    return result
