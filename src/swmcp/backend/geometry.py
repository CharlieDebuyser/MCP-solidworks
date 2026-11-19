"""Géométrie pure, partagée par tous les backends : vecteurs, repères d'esquisse, analyse de contours.

Le contrôle côté serveur avant une fonction (boucles fermées, absence d'intersections, spec §10)
vit ici pour n'être écrit qu'une fois.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Pt2, SketchFrame, SketchSegment, Vec3

TOL = 1e-6  # mm


# --- vecteurs ---------------------------------------------------------------------------------


def add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec3, k: float) -> Vec3:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec3) -> Vec3:
    n = norm(a)
    if n < 1e-12:
        raise ValueError("vecteur nul")
    return (a[0] / n, a[1] / n, a[2] / n)


def dist(a: Vec3, b: Vec3) -> float:
    return norm(sub(a, b))


def angle_deg(a: Vec3, b: Vec3) -> float:
    c = max(-1.0, min(1.0, dot(unit(a), unit(b))))
    return math.degrees(math.acos(c))


AXES: dict[str, Vec3] = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def parse_axis(text: str) -> Vec3 | None:
    """'x', '-y', '+z' → vecteur unitaire ; None si ce n'est pas un nom d'axe."""
    t = text.strip().lower()
    sign = 1.0
    if t[:1] in "+-" and len(t) == 2:
        sign = -1.0 if t[0] == "-" else 1.0
        t = t[1:]
    axis = AXES.get(t)
    return scale(axis, sign) if axis is not None else None


# --- repères d'esquisse -------------------------------------------------------------------------


def frame_for_plane(normal: Vec3, point_on_plane: Vec3 = (0.0, 0.0, 0.0)) -> SketchFrame:
    """Repère déterministe d'un plan : origine = projection de l'origine du modèle sur le plan.

    Convention du backend simulé, alignée sur les plans de référence SolidWorks :
    front → x=+X, y=+Y ; top → x=+X, y=-Z ; right → x=-Z, y=+Y. Le vrai SolidWorks peut choisir
    un autre repère sur une face : seul le repère renvoyé par `sketch_begin` fait foi.
    """
    n = unit(normal)
    up: Vec3 = (0.0, 1.0, 0.0)
    if abs(dot(n, up)) < 0.999:
        y = unit(sub(up, scale(n, dot(up, n))))
        x = cross(y, n)
    else:
        x = (1.0, 0.0, 0.0)
        y = cross(n, x)
    origin = scale(n, dot(point_on_plane, n))
    return SketchFrame(origin=origin, x_axis=x, y_axis=y, normal=n)


PLANE_NORMALS: dict[str, Vec3] = {"front": (0.0, 0.0, 1.0), "top": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0)}


def to_model(f: SketchFrame, x: float, y: float, w: float = 0.0) -> Vec3:
    return add(f.origin, add(scale(f.x_axis, x), add(scale(f.y_axis, y), scale(f.normal, w))))


def to_sketch(f: SketchFrame, p: Vec3) -> Vec3:
    d = sub(p, f.origin)
    return (dot(d, f.x_axis), dot(d, f.y_axis), dot(d, f.normal))


# --- contours 2D ----------------------------------------------------------------------------


def _key(p: Pt2) -> tuple[int, int]:
    return (round(p[0] / (TOL * 10)), round(p[1] / (TOL * 10)))


def arc_sweep(seg: SketchSegment) -> float:
    """Angle balayé signé d'un arc (positif = sens trigonométrique)."""
    assert seg.center is not None and seg.start is not None and seg.end is not None
    a0 = math.atan2(seg.start[1] - seg.center[1], seg.start[0] - seg.center[0])
    a1 = math.atan2(seg.end[1] - seg.center[1], seg.end[0] - seg.center[0])
    d = a1 - a0
    if seg.ccw:
        while d <= TOL * 1e-3:
            d += 2 * math.pi
    else:
        while d >= -TOL * 1e-3:
            d -= 2 * math.pi
    return d


def seg_length(seg: SketchSegment) -> float:
    if seg.type == "line" and seg.start and seg.end:
        return math.dist(seg.start, seg.end)
    if seg.type == "circle" and seg.radius_mm:
        return 2 * math.pi * seg.radius_mm
    if seg.type == "arc" and seg.radius_mm:
        return abs(arc_sweep(seg)) * seg.radius_mm
    return 0.0


def discretize(seg: SketchSegment, per_turn: int = 96) -> list[Pt2]:
    """Polyligne approchant le segment, extrémités incluses."""
    if seg.type == "line" and seg.start and seg.end:
        return [seg.start, seg.end]
    if seg.type == "circle" and seg.center and seg.radius_mm:
        cx, cy = seg.center
        r = seg.radius_mm
        return [
            (cx + r * math.cos(2 * math.pi * i / per_turn), cy + r * math.sin(2 * math.pi * i / per_turn))
            for i in range(per_turn + 1)
        ]
    if seg.type == "arc" and seg.center and seg.start and seg.radius_mm:
        sweep = arc_sweep(seg)
        n = max(2, math.ceil(abs(sweep) / (2 * math.pi) * per_turn))
        a0 = math.atan2(seg.start[1] - seg.center[1], seg.start[0] - seg.center[0])
        cx, cy = seg.center
        r = seg.radius_mm
        return [
            (cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n))
            for i in range(n + 1)
        ]
    return []


def _orient(a: Pt2, b: Pt2, c: Pt2) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _proper_cross(a: Pt2, b: Pt2, c: Pt2, d: Pt2) -> Pt2 | None:
    """Point de croisement franc de [ab] et [cd] (les contacts aux extrémités ne comptent pas)."""
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    eps = 1e-9
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    ):
        t = d1 / (d1 - d2)
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return None


@dataclass
class Loop:
    segments: list[SketchSegment]
    polygon: list[Pt2]  # sommets ordonnés (arcs discrétisés), sans répétition du premier
    area: float  # aire exacte, positive
    centroid: Pt2
    depth: int = 0  # profondeur d'imbrication (0 = contour extérieur)

    def contains(self, p: Pt2) -> bool:
        if len(self.segments) == 1 and self.segments[0].type == "circle":
            s = self.segments[0]
            assert s.center is not None and s.radius_mm is not None
            return math.dist(p, s.center) <= s.radius_mm + TOL
        inside = False
        poly = self.polygon
        j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if (yi > p[1]) != (yj > p[1]) and p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside


@dataclass
class ProfileReport:
    closed: bool
    loops: list[Loop] = field(default_factory=list)
    open_points: list[Pt2] = field(default_factory=list)
    intersections: list[Pt2] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.closed and not self.intersections and not self.duplicates and bool(self.loops)

    @property
    def area(self) -> float:
        return sum(lp.area if lp.depth % 2 == 0 else -lp.area for lp in self.loops)

    @property
    def centroid(self) -> Pt2:
        a = self.area
        if abs(a) < 1e-12:
            return (0.0, 0.0)
        sx = sum((lp.area if lp.depth % 2 == 0 else -lp.area) * lp.centroid[0] for lp in self.loops)
        sy = sum((lp.area if lp.depth % 2 == 0 else -lp.area) * lp.centroid[1] for lp in self.loops)
        return (sx / a, sy / a)

    def contains(self, p: Pt2) -> bool:
        return sum(1 for lp in self.loops if lp.contains(p)) % 2 == 1

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for lp in self.loops for p in lp.polygon]
        ys = [p[1] for lp in self.loops for p in lp.polygon]
        return (min(xs), min(ys), max(xs), max(ys))

    def spans(self, q: Pt2, v: Pt2) -> list[tuple[float, float]]:
        """Paramètres t pour lesquels q + t·v est dans le contour (règle pair-impair)."""
        ts: list[float] = []
        vv = v[0] ** 2 + v[1] ** 2
        for lp in self.loops:
            poly = lp.polygon
            for i in range(len(poly)):
                a, b = poly[i], poly[(i + 1) % len(poly)]
                sa = v[0] * (a[1] - q[1]) - v[1] * (a[0] - q[0])
                sb = v[0] * (b[1] - q[1]) - v[1] * (b[0] - q[0])
                if (sa > 0) == (sb > 0):
                    continue
                u = sa / (sa - sb)
                px, py = a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])
                ts.append(((px - q[0]) * v[0] + (py - q[1]) * v[1]) / vv)
        ts.sort()
        return [(ts[i], ts[i + 1]) for i in range(0, len(ts) - 1, 2)]

    def sample(self, n: int = 9) -> list[tuple[Pt2, float]]:
        """Points intérieurs pondérés : n lignes de balayage, trois points par traversée du contour.

        Robuste pour les contours minces ou en L, où une grille régulière manquerait la matière.
        """
        _, y0, _, y1 = self.bbox()
        out: list[tuple[Pt2, float]] = []
        for j in range(n):
            y = y0 + (y1 - y0) * (j + 0.5) / n
            for lo, hi in self.spans((0.0, y), (1.0, 0.0)):
                if hi - lo < TOL:
                    continue
                out += [((lo + (hi - lo) * f, y), (hi - lo) / 3) for f in (1 / 6, 0.5, 5 / 6)]
        return out


def _loop_metrics(ordered: list[tuple[SketchSegment, bool]]) -> tuple[list[Pt2], float, Pt2]:
    """Polygone, aire exacte et centroïde d'une boucle de lignes et d'arcs."""
    chord: list[Pt2] = []
    poly: list[Pt2] = []
    for seg, rev in ordered:
        pts = discretize(seg)
        if rev:
            pts = pts[::-1]
        chord.append(pts[0])
        poly.extend(pts[:-1])
    # polygone des cordes (formule du lacet)
    a2 = 0.0
    cx = cy = 0.0
    for i in range(len(chord)):
        x0, y0 = chord[i]
        x1, y1 = chord[(i + 1) % len(chord)]
        w = x0 * y1 - x1 * y0
        a2 += w
        cx += (x0 + x1) * w
        cy += (y0 + y1) * w
    area = a2 / 2.0
    mx, my = (cx / 6.0, cy / 6.0)  # moments (aire x centroïde)
    # correction exacte par segment circulaire de chaque arc
    for seg, rev in ordered:
        if seg.type != "arc":
            continue
        assert seg.center is not None and seg.radius_mm is not None and seg.start is not None
        sweep = arc_sweep(seg) * (-1.0 if rev else 1.0)
        theta = abs(sweep)
        r = seg.radius_mm
        seg_area = r * r / 2.0 * (theta - math.sin(theta))
        if seg_area < 1e-15:
            continue
        a_start = math.atan2(seg.start[1] - seg.center[1], seg.start[0] - seg.center[0])
        mid = a_start + arc_sweep(seg) / 2.0
        d = 4.0 * r * math.sin(theta / 2.0) ** 3 / (3.0 * (theta - math.sin(theta)))
        gx, gy = seg.center[0] + d * math.cos(mid), seg.center[1] + d * math.sin(mid)
        s = seg_area if sweep > 0 else -seg_area  # arc trigonométrique : ajoute à l'aire signée
        area += s
        mx += s * gx
        my += s * gy
    if abs(area) < 1e-15:
        return poly, 0.0, chord[0]
    return poly, abs(area), (mx / area, my / area)


def analyze_profile(segments: list[SketchSegment]) -> ProfileReport:
    """Analyse les segments (hors construction et points) d'une esquisse : boucles, ouvertures, croisements."""
    segs = [s for s in segments if not s.construction and s.type != "point"]
    report = ProfileReport(closed=True)
    if not segs:
        report.closed = False
        report.issues.append("l'esquisse ne contient aucun segment de contour")
        return report

    # segments dupliqués ou de longueur nulle
    seen: dict[tuple[object, ...], str] = {}
    for s in segs:
        if seg_length(s) < TOL:
            report.issues.append(f"segment {s.handle} de longueur nulle")
            report.duplicates.append((s.handle, s.handle))
            continue
        if s.type == "circle":
            assert s.center is not None and s.radius_mm is not None
            sig: tuple[object, ...] = ("c", _key(s.center), round(s.radius_mm / TOL))
        else:
            assert s.start is not None and s.end is not None
            ends = tuple(sorted((_key(s.start), _key(s.end))))
            if s.type == "line":
                mid: Pt2 = ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)
            else:
                assert s.center is not None and s.radius_mm is not None
                a_mid = math.atan2(s.start[1] - s.center[1], s.start[0] - s.center[0]) + arc_sweep(s) / 2
                mid = (
                    s.center[0] + s.radius_mm * math.cos(a_mid),
                    s.center[1] + s.radius_mm * math.sin(a_mid),
                )
            sig = (s.type, ends, _key(mid))
        if sig in seen:
            report.duplicates.append((seen[sig], s.handle))
            report.issues.append(f"segments dupliqués : {seen[sig]} et {s.handle}")
        else:
            seen[sig] = s.handle

    # boucles
    open_segs = [s for s in segs if s.type in ("line", "arc") and seg_length(s) >= TOL]
    nodes: dict[tuple[int, int], list[tuple[SketchSegment, bool]]] = {}
    for s in open_segs:
        assert s.start is not None and s.end is not None
        nodes.setdefault(_key(s.start), []).append((s, False))
        nodes.setdefault(_key(s.end), []).append((s, True))
    for lst in nodes.values():
        if len(lst) != 2:
            report.closed = False
            seg0, at_end = lst[0]
            pt = seg0.end if at_end else seg0.start
            assert pt is not None
            report.open_points.append(pt)
            what = "extrémité libre" if len(lst) == 1 else f"{len(lst)} segments se rejoignent"
            report.issues.append(f"{what} en ({pt[0]:.3f}, {pt[1]:.3f}) [{seg0.handle}]")
    for s in segs:
        if s.type == "circle":
            assert s.center is not None and s.radius_mm is not None
            report.loops.append(Loop([s], discretize(s)[:-1], math.pi * s.radius_mm**2, s.center))
    if report.closed:
        unused = {s.handle: s for s in open_segs}
        while unused:
            first = next(iter(unused.values()))
            ordered: list[tuple[SketchSegment, bool]] = []
            cur, rev = first, False
            while True:
                ordered.append((cur, rev))
                del unused[cur.handle]
                tail = cur.start if rev else cur.end
                assert tail is not None
                nxt = [(s, at_end) for s, at_end in nodes[_key(tail)] if s.handle != cur.handle]
                if not nxt or nxt[0][0].handle not in unused:
                    break
                cur, at_end = nxt[0]
                rev = at_end  # on entre par la fin : parcours inversé
            poly, area, centroid = _loop_metrics(ordered)
            report.loops.append(Loop([s for s, _ in ordered], poly, area, centroid))

    # croisements francs entre segments distincts
    polys = [(s, discretize(s, 48)) for s in segs if seg_length(s) >= TOL]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            si, pi = polys[i]
            sj, pj = polys[j]
            hit: Pt2 | None = None
            for a in range(len(pi) - 1):
                for b in range(len(pj) - 1):
                    hit = _proper_cross(pi[a], pi[a + 1], pj[b], pj[b + 1])
                    if hit:
                        break
                if hit:
                    break
            if hit:
                report.intersections.append(hit)
                report.issues.append(
                    f"segments sécants : {si.handle} et {sj.handle} vers ({hit[0]:.3f}, {hit[1]:.3f})"
                )

    # imbrication (contour extérieur / trous)
    for lp in report.loops:
        probe = lp.polygon[0]
        lp.depth = sum(1 for other in report.loops if other is not lp and other.contains(probe))
    return report
