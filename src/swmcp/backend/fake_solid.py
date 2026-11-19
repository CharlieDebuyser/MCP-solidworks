"""Solides du backend simulé : prismes et révolutions combinés par ajout / enlèvement.

Le volume d'une opération se calcule par lancer de rayons : pour un prisme, les intervalles de
matière le long de la normale sont exacts (contours polygonaux, arcs discrétisés finement).
Ce modèle sert aux tests ; il ne reproduit pas le noyau géométrique de SolidWorks.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from . import geometry as g
from .geometry import ProfileReport
from .models import Pt2, SketchFrame, Vec3

Interval = tuple[float, float]
PointMap = Callable[[Vec3], Vec3]


# --- intervalles ------------------------------------------------------------------------------


def i_union(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for lo, hi in sorted(a + b):
        if out and lo <= out[-1][1] + 1e-12:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def i_clip(a: list[Interval], lo: float, hi: float) -> list[Interval]:
    return [(max(x, lo), min(y, hi)) for x, y in a if min(y, hi) - max(x, lo) > 1e-12]


def i_diff(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in sorted(b):
            if bhi <= cur or blo >= hi:
                continue
            if blo > cur:
                out.append((cur, blo))
            cur = max(cur, bhi)
        if hi - cur > 1e-12:
            out.append((cur, hi))
    return out


def i_len(a: list[Interval]) -> float:
    return sum(hi - lo for lo, hi in a)


# --- transformations (répétitions, symétrie) ----------------------------------------------------


def translation(v: Vec3) -> PointMap:
    return lambda p: g.add(p, v)


def rotation(point: Vec3, axis: Vec3, angle_rad: float) -> PointMap:
    k = g.unit(axis)
    c, s = math.cos(angle_rad), math.sin(angle_rad)

    def f(p: Vec3) -> Vec3:
        v = g.sub(p, point)
        r = g.add(g.add(g.scale(v, c), g.scale(g.cross(k, v), s)), g.scale(k, g.dot(k, v) * (1 - c)))
        return g.add(point, r)

    return f


def mirror(point: Vec3, normal: Vec3) -> PointMap:
    n = g.unit(normal)
    return lambda p: g.sub(p, g.scale(n, 2 * g.dot(g.sub(p, point), n)))


def map_frame(f: SketchFrame, t: PointMap) -> SketchFrame:
    o = t(f.origin)

    def vec(v: Vec3) -> Vec3:
        return g.sub(t(g.add(f.origin, v)), o)

    return SketchFrame(origin=o, x_axis=vec(f.x_axis), y_axis=vec(f.y_axis), normal=vec(f.normal))


# --- primitives -------------------------------------------------------------------------------


@dataclass
class Prism:
    frame: SketchFrame
    profile: ProfileReport
    w0: float
    w1: float

    kind = "prism"

    @property
    def nominal_volume(self) -> float:
        return self.profile.area * (self.w1 - self.w0)

    @property
    def centroid(self) -> Vec3:
        c = self.profile.centroid
        return g.to_model(self.frame, c[0], c[1], (self.w0 + self.w1) / 2)

    def inside(self, p: Vec3) -> bool:
        x, y, w = g.to_sketch(self.frame, p)
        return self.w0 - 1e-9 <= w <= self.w1 + 1e-9 and self.profile.contains((x, y))

    def intervals(self, o: Vec3, d: Vec3, t0: float, t1: float) -> list[Interval]:
        ox, oy, ow = g.to_sketch(self.frame, o)
        dx, dy, dw = g.dot(d, self.frame.x_axis), g.dot(d, self.frame.y_axis), g.dot(d, self.frame.normal)
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            if abs(dw) < 1e-12 or not self.profile.contains((ox, oy)):
                return []
            a, b = sorted(((self.w0 - ow) / dw, (self.w1 - ow) / dw))
            return i_clip([(a, b)], t0, t1)
        spans = self.profile.spans((ox, oy), (dx, dy))
        if abs(dw) < 1e-12:
            if not (self.w0 <= ow <= self.w1):
                return []
            return i_clip(spans, t0, t1)
        a, b = sorted(((self.w0 - ow) / dw, (self.w1 - ow) / dw))
        return i_clip(i_clip(spans, a, b), t0, t1)

    def bbox_points(self) -> list[Vec3]:
        return [
            g.to_model(self.frame, x, y, w)
            for lp in self.profile.loops
            if lp.depth == 0
            for x, y in lp.polygon
            for w in (self.w0, self.w1)
        ]

    def transformed(self, t: PointMap) -> Prism:
        return Prism(map_frame(self.frame, t), self.profile, self.w0, self.w1)


@dataclass
class Revolve:
    frame: SketchFrame
    profile: ProfileReport
    axis_point: Pt2
    axis_dir: Pt2  # unitaire, dans le plan de l'esquisse
    angle: float  # radians

    kind = "revolve"

    def _split(self, p: Pt2) -> tuple[float, float]:
        """(coordonnée axiale, distance signée à l'axe) d'un point de l'esquisse."""
        rx, ry = p[0] - self.axis_point[0], p[1] - self.axis_point[1]
        return (rx * self.axis_dir[0] + ry * self.axis_dir[1], -rx * self.axis_dir[1] + ry * self.axis_dir[0])

    @property
    def side(self) -> float:
        return 1.0 if self._split(self.profile.centroid)[1] >= 0 else -1.0

    @property
    def nominal_volume(self) -> float:
        return self.profile.area * abs(self._split(self.profile.centroid)[1]) * self.angle

    @property
    def centroid(self) -> Vec3:
        a, _ = self._split(self.profile.centroid)
        if self.angle < 2 * math.pi - 1e-9:
            c = self.profile.centroid
            return g.to_model(self.frame, c[0], c[1], 0.0)
        return g.to_model(
            self.frame, self.axis_point[0] + a * self.axis_dir[0], self.axis_point[1] + a * self.axis_dir[1]
        )

    def inside(self, p: Vec3) -> bool:
        x, y, w = g.to_sketch(self.frame, p)
        axial, s = self._split((x, y))
        radial = math.hypot(s, w)
        side = self.side
        if self.angle < 2 * math.pi - 1e-9:
            ang = math.atan2(w, s * side) % (2 * math.pi)
            if ang > self.angle + 1e-9:
                return False
        q = (
            self.axis_point[0] + axial * self.axis_dir[0] - side * radial * self.axis_dir[1],
            self.axis_point[1] + axial * self.axis_dir[1] + side * radial * self.axis_dir[0],
        )
        return self.profile.contains(q)

    def intervals(self, o: Vec3, d: Vec3, t0: float, t1: float) -> list[Interval]:
        n = 240
        out: list[Interval] = []
        step = (t1 - t0) / n
        prev_t, prev_in = t0, self.inside(g.add(o, g.scale(d, t0)))
        start = t0 if prev_in else None
        for i in range(1, n + 1):
            t = t0 + i * step
            cur = self.inside(g.add(o, g.scale(d, t)))
            if cur != prev_in:
                lo, hi = prev_t, t
                for _ in range(40):
                    mid = (lo + hi) / 2
                    if self.inside(g.add(o, g.scale(d, mid))) == prev_in:
                        lo = mid
                    else:
                        hi = mid
                edge = (lo + hi) / 2
                if cur:
                    start = edge
                elif start is not None:
                    out.append((start, edge))
                    start = None
            prev_t, prev_in = t, cur
        if start is not None:
            out.append((start, t1))
        return out

    def bbox_points(self) -> list[Vec3]:
        pts: list[Vec3] = []
        steps = 24
        for lp in self.profile.loops:
            if lp.depth:
                continue
            for p in lp.polygon:
                axial, s = self._split(p)
                base = (
                    self.axis_point[0] + axial * self.axis_dir[0],
                    self.axis_point[1] + axial * self.axis_dir[1],
                )
                for i in range(steps + 1):
                    a = self.angle * i / steps
                    q = (
                        base[0] - s * math.cos(a) * self.axis_dir[1],
                        base[1] + s * math.cos(a) * self.axis_dir[0],
                    )
                    pts.append(g.to_model(self.frame, q[0], q[1], s * math.sin(a)))
        return pts

    def transformed(self, t: PointMap) -> Revolve:
        return Revolve(map_frame(self.frame, t), self.profile, self.axis_point, self.axis_dir, self.angle)


Prim = Prism | Revolve
Op = tuple[str, Prim]  # ('+' | '-', primitive)


def material(ops: list[Op], o: Vec3, d: Vec3, t0: float, t1: float) -> list[Interval]:
    """Intervalles de matière le long du rayon o + t·d, après application ordonnée des opérations."""
    cur: list[Interval] = []
    for sign, prim in ops:
        spans = prim.intervals(o, d, t0, t1)
        cur = i_union(cur, spans) if sign == "+" else i_diff(cur, spans)
    return cur


def is_inside(ops: list[Op], p: Vec3) -> bool:
    state = False
    for sign, prim in ops:
        if prim.inside(p):
            state = sign == "+"
    return state


def op_volume(ops: list[Op], sign: str, prim: Prim) -> float:
    """Volume réellement ajouté (signe '+') ou enlevé (signe '-') par `prim` sur le solide courant."""
    samples = prim.profile.sample()
    weight = sum(w for _, w in samples)
    if weight <= 0:
        return 0.0
    if isinstance(prim, Prism):
        total = 0.0
        for (x, y), w in samples:
            o = g.to_model(prim.frame, x, y, 0.0)
            inside = i_len(material(ops, o, prim.frame.normal, prim.w0, prim.w1))
            total += w * (inside if sign == "-" else (prim.w1 - prim.w0) - inside)
        return prim.profile.area * total / weight
    # révolution : fraction pondérée de points échantillonnés dans la matière
    hits = 0.0
    steps = 12
    for (x, y), w in samples:
        axial, s = prim._split((x, y))
        base = (prim.axis_point[0] + axial * prim.axis_dir[0], prim.axis_point[1] + axial * prim.axis_dir[1])
        for i in range(steps):
            a = prim.angle * (i + 0.5) / steps
            q = (base[0] - s * math.cos(a) * prim.axis_dir[1], base[1] + s * math.cos(a) * prim.axis_dir[0])
            if is_inside(ops, g.to_model(prim.frame, q[0], q[1], s * math.sin(a))):
                hits += w / steps
    frac = hits / weight
    return prim.nominal_volume * (frac if sign == "-" else 1.0 - frac)
