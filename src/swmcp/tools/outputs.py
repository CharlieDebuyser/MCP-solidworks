"""Modèles de sortie propres aux outils (le reste vient de `backend.models`)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..apiindex import ApiDoc, ApiHit
from ..backend.models import (
    BBox,
    DimInfo,
    DocRef,
    EdgeInfo,
    FaceInfo,
    FeatureNode,
    InterferencePair,
    RebuildIssue,
    SketchEntityRef,
)
from ..safety.checkpoints import Checkpoint
from .registry import Page


class StatusResult(BaseModel):
    connected: bool
    responsive: bool
    suspect: bool = Field(
        description="Vrai après un délai dépassé, tant que SolidWorks n'a pas répondu de nouveau"
    )
    server_version: str
    tool_schema_version: str
    backend: str
    profile: str
    read_only: bool
    workspace: str
    allowed_dirs: list[str]
    timeout_s: float
    auto_reconnect: bool
    raw_api: bool
    api_indexed: bool
    queue_pending: int


class DocList(Page):
    documents: list[DocRef]


class SavedResult(BaseModel):
    path: str
    overwritten: bool = False


class ClosedResult(BaseModel):
    closed: str
    saved: bool


class PropsResult(BaseModel):
    properties: dict[str, str]


class CheckpointList(Page):
    checkpoints: list[Checkpoint]


class CheckpointRestored(BaseModel):
    checkpoint: Checkpoint
    opened: DocRef


class SketchEntities(BaseModel):
    sketch: str | None
    entities: list[SketchEntityRef]


class Verification(BaseModel):
    rebuild_ok: bool | None
    feature_count: int | None
    body_count: int
    volume_mm3: float
    volume_delta_mm3: float
    bbox_mm: BBox | None = None
    face_count: int | None = None
    edge_count: int | None = None


class FeatureResult(BaseModel):
    feature: str
    type: str
    dimensions: list[str] = Field(default_factory=list, description="Cotes nommées à passer à dimension_set")
    sketch: str | None = None
    volume_before_mm3: float
    volume_after_mm3: float
    verify: Verification


class DimensionSetResult(BaseModel):
    full_name: str
    old_value: float
    new_value: float
    unit: str
    rebuild_ok: bool
    volume_before_mm3: float
    volume_after_mm3: float
    mass_before_g: float
    mass_after_g: float


class VariableSetResult(BaseModel):
    name: str
    old_value: str | None
    new_value: str
    rebuild_ok: bool | None


class FeatureModified(BaseModel):
    feature: str
    suppressed: bool | None = None
    rebuild_ok: bool | None = None


class FeaturesDeleted(BaseModel):
    deleted: list[str]
    rebuild_ok: bool | None
    checkpoint: str | None = None


class TreePage(Page):
    items: list[FeatureNode]


class DimPage(Page):
    items: list[DimInfo]


class FacePage(Page):
    items: list[FaceInfo]


class EdgePage(Page):
    items: list[EdgeInfo]


class InterferenceResult(BaseModel):
    count: int
    pairs: list[InterferencePair]


class RuleResult(BaseModel):
    rule: str
    expected: Any
    measured: Any
    passed: bool
    message: str | None = None


class CheckDesignResult(BaseModel):
    passed: bool
    results: list[RuleResult]


class RebuildIssues(BaseModel):
    errors: list[RebuildIssue]


class CaptureResult(BaseModel):
    views: list[str]
    images: int
    width_px: int
    height_px: int
    grid: bool


class ExportResult(BaseModel):
    path: str
    format: str
    size_bytes: int
    duration_ms: int
    overwritten: bool


class ApiSearchResult(Page):
    indexed: bool
    results: list[ApiHit]


class ApiDescribeResult(BaseModel):
    indexed: bool
    found: bool
    member: ApiDoc | None = None


class KnowledgeResult(BaseModel):
    topic: str
    title: str
    version: str | None
    content: str
    available: list[str]


RuleName = Literal[
    "no_rebuild_errors",
    "sketches_fully_defined",
    "single_body",
    "bbox_within",
    "volume_within",
    "has_material",
]
