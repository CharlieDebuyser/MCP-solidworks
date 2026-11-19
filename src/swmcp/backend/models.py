"""Données sérialisables échangées entre les outils et le backend.

Aucun objet COM ne franchit cette frontière. Unités : mm, mm², mm³, g, degrés.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Vec3 = tuple[float, float, float]
Pt2 = tuple[float, float]

DocType = Literal["part", "assembly", "drawing"]
SketchStatus = Literal["under_defined", "fully_defined", "over_defined", "failed"]
EndCondition = Literal["blind", "mid_plane", "through_all", "up_to_next"]
Direction = Literal["normal", "reverse", "both"]
RelationType = Literal[
    "horizontal",
    "vertical",
    "coincident",
    "tangent",
    "equal",
    "parallel",
    "perpendicular",
    "concentric",
    "symmetric",
    "fix",
    "midpoint",
]
DimensionType = Literal["length", "diameter", "radius", "angle", "distance"]
ExportFormat = Literal["step", "stl", "iges", "pdf", "dxf", "png"]
ViewName = Literal["iso", "front", "top", "right", "back", "bottom", "left", "current"]


# --- session et documents -------------------------------------------------------------------


class InstanceInfo(BaseModel):
    pid: int
    title: str | None = None


class ConnectResult(BaseModel):
    version: str = Field(description="Année de la version, ex. '2025'")
    revision: str = Field(description="RevisionNumber brut, ex. '33.1.0'")
    pid: int
    language: str | None = None
    active_doc: str | None = None
    instances: list[InstanceInfo] = Field(default_factory=list)


class DocRef(BaseModel):
    id: str
    name: str
    path: str | None = None
    type: DocType
    dirty: bool = False
    read_only: bool = False
    active: bool = False


class DocOpenResult(DocRef):
    open_errors: list[str] = Field(default_factory=list)
    open_warnings: list[str] = Field(default_factory=list)


class DocInfo(DocRef):
    material: str | None = None
    configuration: str | None = None
    length_unit: str = "mm"
    mass_unit: str = "g"
    angle_unit: str = "deg"


# --- esquisse -------------------------------------------------------------------------------


class SketchFrame(BaseModel):
    """Repère de l'esquisse exprimé dans le repère du modèle (origine en mm)."""

    origin: Vec3
    x_axis: Vec3
    y_axis: Vec3
    normal: Vec3


class SketchBeginResult(BaseModel):
    name: str
    plane: str
    frame: SketchFrame


class SketchEndResult(BaseModel):
    name: str
    status: SketchStatus
    closed: bool
    segment_count: int
    issues: list[str] = Field(default_factory=list)


class SketchEntityRef(BaseModel):
    handle: str
    type: Literal["line", "circle", "arc", "point"]
    role: str | None = Field(default=None, description="Rôle dans la primitive : start, end, center, bottom…")


class SketchSegment(BaseModel):
    handle: str
    type: Literal["line", "circle", "arc", "point"]
    start: Pt2 | None = None
    end: Pt2 | None = None
    center: Pt2 | None = None
    radius_mm: float | None = None
    ccw: bool = True
    construction: bool = False


class RelationInfo(BaseModel):
    type: str
    entities: list[str]


class DimInfo(BaseModel):
    full_name: str
    value: float
    unit: Literal["mm", "deg"] = "mm"
    feature: str | None = None


class RelationResult(BaseModel):
    relation: str
    entities: list[str]
    status: SketchStatus


class DimensionResult(BaseModel):
    full_name: str
    value: float
    unit: Literal["mm", "deg"]
    status: SketchStatus


class SketchInspectResult(BaseModel):
    name: str
    status: SketchStatus
    closed: bool
    frame: SketchFrame
    segments: list[SketchSegment]
    relations: list[RelationInfo]
    dimensions: list[DimInfo]
    issues: list[str] = Field(default_factory=list)


# --- fonctions ------------------------------------------------------------------------------


class BBox(BaseModel):
    min: Vec3
    max: Vec3
    size: Vec3


class BodyStats(BaseModel):
    body_count: int = 0
    volume_mm3: float = 0.0
    face_count: int = 0
    edge_count: int = 0
    bbox_mm: BBox | None = None


class FeatureCreated(BaseModel):
    """Retour du backend après création d'une fonction."""

    feature: str
    type: str
    dimensions: list[str] = Field(default_factory=list, description="Cotes nommées pilotant la fonction")
    sketch: str | None = None


class FeatureNode(BaseModel):
    name: str
    type: str
    suppressed: bool = False
    status: Literal["ok", "warning", "error"] = "ok"
    message: str | None = None
    parent: str | None = None
    lightweight: bool | None = None


class RebuildIssue(BaseModel):
    feature: str
    type: str
    severity: Literal["error", "warning"]
    code: int | None = None
    message: str


class RebuildResult(BaseModel):
    rebuild_ok: bool
    errors: list[RebuildIssue] = Field(default_factory=list)
    warnings: list[RebuildIssue] = Field(default_factory=list)
    under_defined_sketches: list[str] = Field(default_factory=list)
    dangling_references: list[str] = Field(default_factory=list)


class MassResult(BaseModel):
    volume_mm3: float
    surface_area_mm2: float
    mass_g: float
    center_of_mass_mm: Vec3 | None
    bbox_mm: BBox | None
    material: str | None
    density_g_cm3: float | None
    body_count: int


class FaceInfo(BaseModel):
    handle: str
    surface: Literal["plane", "cylinder", "cone", "sphere", "torus", "other"]
    area_mm2: float
    centroid_mm: Vec3
    normal: Vec3 | None = None
    radius_mm: float | None = None
    feature: str | None = None


class EdgeInfo(BaseModel):
    handle: str
    curve: Literal["line", "circle", "arc", "other"]
    length_mm: float
    midpoint_mm: Vec3
    start_mm: Vec3 | None = None
    end_mm: Vec3 | None = None
    center_mm: Vec3 | None = None
    radius_mm: float | None = None
    faces: list[str] = Field(default_factory=list, description="Handles des faces adjacentes")


class MeasureResult(BaseModel):
    distance_mm: float
    angle_deg: float | None = None
    a: str
    b: str


class InterferencePair(BaseModel):
    a: str
    b: str
    volume_mm3: float


class DimensionChange(BaseModel):
    full_name: str
    old_value: float
    new_value: float
    unit: Literal["mm", "deg"]


class VariableChange(BaseModel):
    name: str
    old_value: str | None
    new_value: str


class PropChange(BaseModel):
    name: str
    before: str | None
    after: str | None


class RawCallResult(BaseModel):
    target: str
    method: str
    result: Any = None
