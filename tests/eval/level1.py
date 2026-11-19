"""`sw-bench` niveau 1 : dix tâches simples jouées par une séquence d'appels scriptée, sans LLM (spec §12, §13 M2).

Chaque tâche a un énoncé, des attendus mesurables et un script ; la notation est indépendante du
script : elle mesure le modèle produit (boîte englobante ±0,1 mm, volume ±1 %, positions ±0,1 mm).
Les mêmes tâches serviront d'énoncés au jeu d'évaluation joué avec un LLM réel.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Call = Callable[..., Awaitable[dict[str, Any]]]
PI = math.pi


@dataclass
class Task:
    id: str
    statement: str
    bbox_mm: tuple[float, float, float]
    volume_mm3: float
    script: Callable[[Call], Awaitable[None]]
    hole_centers_mm: list[tuple[float, float, float]] = field(default_factory=list)
    hole_radius_mm: float | None = None
    body_count: int = 1


async def _rect_block(ok: Call, x: float, y: float, z: float, plane: str = "front") -> None:
    await ok("doc_new", type="part")
    await ok("sketch_begin", plane=plane, name="Base_esq")
    r = await ok("sketch_add_rect", x1_mm=0, y1_mm=0, x2_mm=x, y2_mm=y)
    by = {e["role"]: e["handle"] for e in r["entities"]}
    await ok("sketch_add_relation", type="coincident", entities=[by["bottom_left"], "origin"])
    await ok("sketch_add_dimension", type="length", entities=[by["bottom"]], value=x, name="Longueur")
    await ok("sketch_add_dimension", type="length", entities=[by["right"]], value=y, name="Largeur")
    end = await ok("sketch_end")
    assert end["status"] == "fully_defined" and end["closed"]
    await ok("feature_extrude", sketch="Base_esq", depth_mm=z, name="Base")


async def _face(ok: Call, normal: list[float], extreme: str) -> str:
    handle: str = (await ok("find_faces", planar=True, normal=normal, extreme=extreme, limit=1))["items"][0][
        "handle"
    ]
    return handle


async def plate_4_holes(ok: Call) -> None:
    await _rect_block(ok, 100, 60, 5)
    top = await _face(ok, [0, 0, 1], "max_z")
    pts = [[10, 10, 5], [90, 10, 5], [90, 50, 5], [10, 50, 5]]
    await ok("feature_hole", face=top, points_mm=pts, diameter_mm=6, through=True, name="Percages")


async def stepped_cylinder(ok: Call) -> None:
    await ok("doc_new", type="part")
    await ok("sketch_begin", plane="front", name="Profil")
    axis = (await ok("sketch_add_line", x1_mm=0, y1_mm=0, x2_mm=0, y2_mm=50, construction=True))["entities"][
        0
    ]["handle"]
    await ok(
        "sketch_add_polyline", points_mm=[[0, 0], [15, 0], [15, 20], [10, 20], [10, 50], [0, 50]], closed=True
    )
    await ok("sketch_end")
    await ok("feature_revolve", sketch="Profil", axis=axis, name="Corps")


async def l_bracket(ok: Call) -> None:
    await ok("doc_new", type="part")
    await ok("sketch_begin", plane="front", name="Profil")
    await ok(
        "sketch_add_polyline", points_mm=[[0, 0], [60, 0], [60, 3], [3, 3], [3, 40], [0, 40]], closed=True
    )
    await ok("sketch_end")
    await ok("feature_extrude", sketch="Profil", depth_mm=30, name="Equerre")
    wing = (await ok("find_faces", planar=True, normal=[0, 1, 0], near_point_mm=[50, 3, 15], limit=1))[
        "items"
    ][0]["handle"]
    await ok(
        "feature_hole",
        face=wing,
        points_mm=[[50, 3, 10], [50, 3, 20]],
        diameter_mm=5,
        through=True,
        name="Percages",
    )


async def box_with_pocket(ok: Call) -> None:
    await _rect_block(ok, 50, 40, 20, plane="top")
    top = await _face(ok, [0, 1, 0], "max_y")
    frame = (await ok("sketch_begin", plane=top, name="Poche_esq"))["frame"]

    # coins de la poche donnés dans le repère du modèle, convertis avec le repère renvoyé par sketch_begin
    def to_sketch(p: list[float]) -> tuple[float, float]:
        d = [p[i] - frame["origin"][i] for i in range(3)]
        return (
            sum(d[i] * frame["x_axis"][i] for i in range(3)),
            sum(d[i] * frame["y_axis"][i] for i in range(3)),
        )

    (x1, y1), (x2, y2) = to_sketch([10, 20, -10]), to_sketch([40, 20, -30])
    await ok("sketch_add_rect", x1_mm=x1, y1_mm=y1, x2_mm=x2, y2_mm=y2)
    await ok("sketch_end")
    await ok("feature_cut_extrude", sketch="Poche_esq", depth_mm=10, name="Poche")


async def washer(ok: Call) -> None:
    await ok("doc_new", type="part")
    await ok("sketch_begin", plane="front", name="Profil")
    await ok("sketch_add_circle", cx_mm=0, cy_mm=0, diameter_mm=30)
    await ok("sketch_add_circle", cx_mm=0, cy_mm=0, diameter_mm=12)
    await ok("sketch_end")
    await ok("feature_extrude", sketch="Profil", depth_mm=3, name="Rondelle")


async def slotted_plate(ok: Call) -> None:
    await ok("doc_new", type="part")
    await ok("sketch_begin", plane="front", name="Profil")
    await ok("sketch_add_rect", x1_mm=0, y1_mm=0, x2_mm=80, y2_mm=30)
    await ok("sketch_add_slot", x1_mm=20, y1_mm=15, x2_mm=60, y2_mm=15, width_mm=8)
    await ok("sketch_end")
    await ok("feature_extrude", sketch="Profil", depth_mm=4, name="Plaque")


async def chamfered_block(ok: Call) -> None:
    await _rect_block(ok, 40, 30, 10)
    edge = (await ok("find_edges", kind="linear", length_range_mm=[39.9, 40.1], extreme="max_z", limit=1))[
        "items"
    ][0]
    await ok("feature_chamfer", edges=[edge["handle"]], distance_mm=2, name="Chanfrein")


async def filleted_block(ok: Call) -> None:
    await _rect_block(ok, 40, 30, 10)
    edges = (await ok("find_edges", kind="linear", length_range_mm=[9.9, 10.1]))["items"]
    await ok("feature_fillet", edges=[e["handle"] for e in edges], radius_mm=3, name="Conges")


async def plate_linear_pattern(ok: Call) -> None:
    await _rect_block(ok, 120, 40, 6)
    top = await _face(ok, [0, 0, 1], "max_z")
    await ok("feature_hole", face=top, points_mm=[[15, 20, 6]], diameter_mm=8, through=True, name="Trou")
    await ok(
        "feature_pattern_linear", features=["Trou"], direction="x", count=4, spacing_mm=30, name="Rangee"
    )


async def flange_circular_pattern(ok: Call) -> None:
    await ok("doc_new", type="part")
    await ok("sketch_begin", plane="front", name="Disque_esq")
    await ok("sketch_add_circle", cx_mm=0, cy_mm=0, diameter_mm=80)
    await ok("sketch_end")
    await ok("feature_extrude", sketch="Disque_esq", depth_mm=8, name="Disque")
    top = await _face(ok, [0, 0, 1], "max_z")
    await ok("feature_hole", face=top, points_mm=[[30, 0, 8]], diameter_mm=6, through=True, name="Trou")
    await ok("feature_pattern_circular", features=["Trou"], axis="z", count=6, name="Couronne")


TASKS: list[Task] = [
    Task(
        "L1-01",
        "Plaque 100 × 60 × 5 mm avec quatre perçages débouchants Ø6 à 10 mm des bords.",
        (100, 60, 5),
        30000 - 4 * PI * 9 * 5,
        plate_4_holes,
        [(10, 10, 5), (90, 10, 5), (90, 50, 5), (10, 50, 5)],
        3,
    ),
    Task(
        "L1-02",
        "Cylindre étagé d'axe Y : Ø30 sur 20 mm puis Ø20 sur 30 mm.",
        (30, 50, 30),
        PI * (225 * 20 + 100 * 30),
        stepped_cylinder,
    ),
    Task(
        "L1-03",
        "Support en L 60 × 40 mm, épaisseur 3 mm, largeur 30 mm, deux perçages Ø5 à 10 mm des bords.",
        (60, 40, 30),
        291 * 30 - 2 * PI * 6.25 * 3,
        l_bracket,
        [(50, 3, 10), (50, 3, 20)],
        2.5,
    ),
    Task(
        "L1-04",
        "Bloc 50 × 20 × 40 mm (hauteur 20 selon Y) avec une poche de 30 × 20 mm, profonde de 10 mm, sur le dessus.",
        (50, 20, 40),
        40000 - 6000,
        box_with_pocket,
    ),
    Task(
        "L1-05",
        "Rondelle Ø30 extérieur, Ø12 intérieur, épaisseur 3 mm.",
        (30, 30, 3),
        PI * (225 - 36) * 3,
        washer,
    ),
    Task(
        "L1-06",
        "Plaque 80 × 30 × 4 mm avec une fente oblongue de 8 mm de large, entraxe 40 mm, centrée.",
        (80, 30, 4),
        (2400 - 320 - PI * 16) * 4,
        slotted_plate,
    ),
    Task(
        "L1-07",
        "Bloc 40 × 30 × 10 mm avec un chanfrein de 2 mm sur une arête longue du dessus.",
        (40, 30, 10),
        12000 - 80,
        chamfered_block,
    ),
    Task(
        "L1-08",
        "Bloc 40 × 30 × 10 mm avec des congés de 3 mm sur les quatre arêtes verticales.",
        (40, 30, 10),
        12000 - 4 * (1 - PI / 4) * 9 * 10,
        filleted_block,
    ),
    Task(
        "L1-09",
        "Plaque 120 × 40 × 6 mm avec quatre perçages Ø8 alignés, pas de 30 mm, premier à 15 mm du bord.",
        (120, 40, 6),
        28800 - 4 * PI * 16 * 6,
        plate_linear_pattern,
        [(15, 20, 6), (45, 20, 6), (75, 20, 6), (105, 20, 6)],
        4,
    ),
    Task(
        "L1-10",
        "Bride Ø80, épaisseur 8 mm, six perçages Ø6 répartis sur un cercle Ø60.",
        (80, 80, 8),
        PI * 1600 * 8 - 6 * PI * 9 * 8,
        flange_circular_pattern,
        [(30 * math.cos(i * PI / 3), 30 * math.sin(i * PI / 3), 8) for i in range(6)],
        3,
    ),
]


async def grade(ok: Call, task: Task) -> list[str]:
    """Notation indépendante du script : mesure le modèle et renvoie la liste des écarts (vide = réussite)."""
    problems: list[str] = []
    check = await ok(
        "check_design",
        rules=[
            {"rule": "no_rebuild_errors"},
            {"rule": "single_body"},
            {"rule": "bbox_within", "size_mm": list(task.bbox_mm), "tolerance_mm": 0.1},
            {"rule": "volume_within", "volume_mm3": task.volume_mm3, "tolerance_pct": 1.0},
        ],
    )
    problems += [
        f"{r['rule']} : attendu {r['expected']}, mesuré {r['measured']}"
        for r in check["results"]
        if not r["passed"]
    ]
    for center in task.hole_centers_mm:
        found = await ok(
            "find_edges", kind="circular", radius_mm=task.hole_radius_mm, near_point_mm=list(center), limit=1
        )
        got = found["items"][0]["center_mm"] if found["items"] else None
        if got is None or math.dist(got, center) > 0.1:
            problems.append(f"perçage attendu en {center}, trouvé en {got}")
    return problems
