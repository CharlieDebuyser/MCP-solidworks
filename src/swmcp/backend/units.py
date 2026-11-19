"""Conversions mm/deg <-> m/rad (spec §5, §10).

Seul endroit du code où figurent les facteurs de conversion : l'API SolidWorks travaille en
mètres, radians, m², m³ et kg ; la frontière MCP en mm, degrés, mm², mm³ et g.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

_MM_PER_M = 1000.0


def mm_to_m(v: float) -> float:
    return v / _MM_PER_M


def m_to_mm(v: float) -> float:
    return v * _MM_PER_M


def deg_to_rad(v: float) -> float:
    return math.radians(v)


def rad_to_deg(v: float) -> float:
    return math.degrees(v)


def m2_to_mm2(v: float) -> float:
    return v * _MM_PER_M**2


def m3_to_mm3(v: float) -> float:
    return v * _MM_PER_M**3


def mm3_to_m3(v: float) -> float:
    return v / _MM_PER_M**3


def kg_to_g(v: float) -> float:
    return v * 1000.0


def kg_m3_to_g_cm3(v: float) -> float:
    return v / 1000.0


def g_cm3_to_g_mm3(v: float) -> float:
    return v / 1000.0


def pt_mm_to_m(p: Sequence[float]) -> tuple[float, ...]:
    return tuple(mm_to_m(c) for c in p)


def vec3_m_to_mm(p: Iterable[float]) -> tuple[float, float, float]:
    x, y, z = (m_to_mm(c) for c in p)
    return (x, y, z)


def vec3_mm_to_m(p: Iterable[float]) -> tuple[float, float, float]:
    x, y, z = (mm_to_m(c) for c in p)
    return (x, y, z)


def rnd(v: float, digits: int = 6) -> float:
    """Arrondi de présentation : supprime le bruit flottant et les -0.0."""
    r = round(v, digits)
    return 0.0 if r == 0 else r


def rnd3(p: Iterable[float], digits: int = 6) -> tuple[float, float, float]:
    x, y, z = (rnd(c, digits) for c in p)
    return (x, y, z)
