"""`FakeSwBackend` : simulation en mémoire de SolidWorks pour les tests (spec §12).

Modèle paramétrique rejouable : documents, esquisses, fonctions, volumes exacts pour les prismes.
Il ne reproduit ni les comportements ni les échecs du vrai logiciel ; les tests d'intégration
restent la référence. Les noms par défaut imitent une installation française.
"""

from __future__ import annotations

import copy
import io
import math
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..envelope import DocState, ErrorCode, SwError, invalid
from . import fake_solid as fs
from . import geometry as g
from .composite import compose_hole
from .entities import EntityRegistry, stale
from .models import (
    BBox,
    BodyStats,
    ConnectResult,
    DimensionChange,
    DimensionResult,
    DimInfo,
    DocInfo,
    DocOpenResult,
    DocRef,
    EdgeInfo,
    FaceInfo,
    FeatureCreated,
    FeatureNode,
    InstanceInfo,
    InterferencePair,
    MassResult,
    MeasureResult,
    PropChange,
    Pt2,
    RebuildIssue,
    RebuildResult,
    RelationInfo,
    RelationResult,
    SketchBeginResult,
    SketchEndResult,
    SketchEntityRef,
    SketchFrame,
    SketchInspectResult,
    SketchSegment,
    SketchStatus,
    VariableChange,
    Vec3,
)
from .units import rnd, rnd3

MAGIC = b"SWMCP-FAKE"
MATERIALS: dict[str, float] = {"Acier": 7.85, "Aluminium 6061": 2.70, "Inox 304": 8.00, "ABS": 1.05}
PLANE_NAMES = {"front": "Plan de face", "top": "Plan de dessus", "right": "Plan de droite"}
EXT_TYPE = {".sldprt": "part", ".sldasm": "assembly", ".slddrw": "drawing"}
TYPE_EXT = {v: k for k, v in EXT_TYPE.items()}
REL_COST = {
    "tangent": 1,
    "equal": 1,
    "parallel": 1,
    "perpendicular": 1,
    "concentric": 2,
    "symmetric": 2,
    "midpoint": 2,
}
REL_MIN = {"symmetric": 3}
VIEW_DIRS: dict[str, tuple[Vec3, Vec3]] = {  # (droite, haut) de l'image
    "front": ((1, 0, 0), (0, 1, 0)),
    "back": ((-1, 0, 0), (0, 1, 0)),
    "top": ((1, 0, 0), (0, 0, -1)),
    "bottom": ((1, 0, 0), (0, 0, 1)),
    "right": ((0, 0, -1), (0, 1, 0)),
    "left": ((0, 0, 1), (0, 1, 0)),
    "iso": ((0.7071, 0, -0.7071), (-0.4082, 0.8165, -0.4082)),
}
VIEW_DIRS["current"] = VIEW_DIRS["iso"]

Key = tuple[Any, ...]


@dataclass
class FPoint:
    id: int
    x: float
    y: float
    fixed: bool = False


@dataclass
class FSeg:
    id: int
    type: str
    a: int | None = None
    b: int | None = None
    c: int | None = None
    radius: float = 0.0
    ccw: bool = True
    construction: bool = False


@dataclass
class FDim:
    name: str
    kind: str
    ents: list[Any]
    value: float
    unit: str


@dataclass
class FSketch:
    uid: int
    plane: str
    face_key: Key | None
    frame: SketchFrame
    points: dict[int, FPoint] = field(default_factory=dict)
    segs: dict[int, FSeg] = field(default_factory=dict)
    relations: list[tuple[str, list[Any]]] = field(default_factory=list)
    dims: list[FDim] = field(default_factory=list)
    alias: dict[int, int] = field(default_factory=dict)
    cost: int = 0
    next_id: int = 1


@dataclass
class FFeature:
    uid: int
    name: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    dims: dict[str, float] = field(default_factory=dict)
    dim_units: dict[str, str] = field(default_factory=dict)
    sketch: FSketch | None = None
    suppressed: bool = False
    error: str | None = None
    hint: str | None = None
    delta: float = 0.0


@dataclass
class FFace:
    key: Key
    surface: str
    area: float
    centroid: Vec3
    normal: Vec3 | None
    radius: float | None
    feature: int


@dataclass
class FEdge:
    key: Key
    curve: str
    length: float
    pts: list[Vec3]
    center: Vec3 | None
    radius: float | None
    faces: list[Key]
    feature: int


@dataclass
class FComponent:
    name: str
    bbox: tuple[Vec3, Vec3]
    lightweight: bool = False


@dataclass
class FDoc:
    id: str
    name: str
    type: str
    path: str | None = None
    dirty: bool = False
    read_only: bool = False
    features: list[FFeature] = field(default_factory=list)
    open_sketch: int | None = None
    props: dict[str, str] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)
    material: str | None = None
    components: list[FComponent] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    next_uid: int = 1
    # état reconstruit
    ops: list[fs.Op] = field(default_factory=list)
    faces: dict[Key, FFace] = field(default_factory=dict)
    edges: dict[Key, FEdge] = field(default_factory=dict)
    volume: float = 0.0
    moment: Vec3 = (0.0, 0.0, 0.0)
    bbox_pts: list[Vec3] = field(default_factory=list)
    bodies: int = 0
    op_index: dict[int, tuple[int, int]] = field(default_factory=dict)


def _bbox(pts: list[Vec3]) -> BBox | None:
    if not pts:
        return None
    lo = (min(p[0] for p in pts), min(p[1] for p in pts), min(p[2] for p in pts))
    hi = (max(p[0] for p in pts), max(p[1] for p in pts), max(p[2] for p in pts))
    return BBox(min=rnd3(lo), max=rnd3(hi), size=rnd3(g.sub(hi, lo)))


class FakeSwBackend:
    """Implémente `SwBackend` sans SolidWorks."""

    def __init__(self, revision: str = "33.1.0", pid: int = 4242) -> None:
        self.revision = revision
        self.pid = pid
        self._connected = False
        self._lost = False
        self.docs: list[FDoc] = []
        self.active: FDoc | None = None
        self.saved: dict[str, FDoc] = {}
        self.registry = EntityRegistry()
        self.faults: deque[SwError | float] = deque()
        self.default_template = True
        self._doc_seq = 0
        self.cleanup_calls = 0

    # --- aides de test ---------------------------------------------------------------------
    def inject(self, code: ErrorCode, message: str = "panne injectée", times: int = 1) -> None:
        for _ in range(times):
            self.faults.append(SwError(code, message))

    def inject_hang(self, seconds: float) -> None:
        self.faults.append(seconds)

    def kill(self) -> None:
        """Simule la fermeture forcée de SolidWorks : documents perdus, prochain appel en échec."""
        self._lost = True
        self.docs.clear()
        self.active = None

    def set_material(self, name: str | None) -> None:
        self._doc().material = name

    def add_component(self, name: str, lo: Vec3, hi: Vec3, lightweight: bool = False) -> None:
        self._doc().components.append(FComponent(name, (lo, hi), lightweight))

    # --- session ---------------------------------------------------------------------------
    def _tick(self) -> None:
        if self._lost:
            raise SwError(ErrorCode.SW_DISCONNECTED, "L'instance SolidWorks ne répond plus (0x800706BA).")
        if not self._connected:
            raise SwError(ErrorCode.NOT_CONNECTED, "Aucune connexion à SolidWorks.")
        if self.faults:
            fault = self.faults.popleft()
            if isinstance(fault, SwError):
                raise fault
            time.sleep(fault)

    def connect(self, launch_if_needed: bool = False, pid: int | None = None) -> ConnectResult:
        if pid is not None and pid != self.pid:
            raise invalid(
                f"Aucune instance SolidWorks avec le pid {pid}.",
                hint=f"Instances disponibles : pid {self.pid}.",
            )
        self._lost = False
        self._connected = True
        return ConnectResult(
            version=str(int(self.revision.split(".")[0]) + 1992),
            revision=self.revision,
            pid=self.pid,
            language="french",
            active_doc=self.active.name if self.active else None,
            instances=[InstanceInfo(pid=self.pid, title="SOLIDWORKS (simulé)")],
        )

    def is_connected(self) -> bool:
        # Comme en COM : une référence morte ne se découvre qu'à l'appel suivant.
        return self._connected

    def ping(self) -> bool:
        self._tick()
        return True

    def mark_disconnected(self) -> None:
        self._connected = False
        self.registry.clear()

    def shutdown(self) -> None:
        self._connected = False

    def state(self) -> DocState:
        d = self.active
        if not self._connected or self._lost or d is None:
            return DocState()
        sk = self._feat_by_uid(d, d.open_sketch) if d.open_sketch else None
        return DocState(
            doc=d.name,
            doc_type=d.type,
            dirty=d.dirty,
            rebuild_ok=not any(f.error for f in d.features),
            feature_count=len(d.features) + (4 if d.type == "part" else 0),
            sketch_open=sk.name if sk else None,
        )

    def cleanup(self) -> None:
        self.cleanup_calls += 1

    # --- documents -------------------------------------------------------------------------
    def _doc(self, part: bool = False) -> FDoc:
        self._tick()
        if self.active is None:
            raise SwError(ErrorCode.NO_ACTIVE_DOC, "Aucun document actif.")
        if part and self.active.type != "part":
            raise SwError(
                ErrorCode.WRONG_DOC_TYPE,
                f"Cet outil exige une pièce ; le document actif est de type {self.active.type}.",
            )
        return self.active

    def _ref(self, d: FDoc) -> DocRef:
        return DocRef(
            id=d.id,
            name=d.name,
            path=d.path,
            type=d.type,
            dirty=d.dirty,
            read_only=d.read_only,
            active=d is self.active,
        )

    def list_docs(self) -> list[DocRef]:
        self._tick()
        return [self._ref(d) for d in self.docs]

    def _add_doc(self, d: FDoc) -> FDoc:
        self.docs.append(d)
        self.active = d
        return d

    def new_doc(self, doc_type: str, template_path: str | None) -> DocRef:
        self._tick()
        if template_path is None and not self.default_template:
            raise invalid(
                "Aucun modèle (template) par défaut n'est configuré dans SolidWorks.",
                hint="Fournir template_path (.prtdot, .asmdot ou .drwdot).",
            )
        self._doc_seq += 1
        label = {"part": "Pièce", "assembly": "Assemblage", "drawing": "Mise en plan"}[doc_type]
        return self._ref(
            self._add_doc(FDoc(id=f"doc{self._doc_seq}", name=f"{label}{self._doc_seq}", type=doc_type))
        )

    def open_doc(self, path: str, read_only: bool) -> DocOpenResult:
        self._tick()
        p = Path(path)
        for d in self.docs:
            if d.path == path:
                self.active = d
                return DocOpenResult(**self._ref(d).model_dump(), open_warnings=["document déjà ouvert"])
        doc_type = EXT_TYPE.get(p.suffix.lower())
        if doc_type is None:
            raise invalid(f"Extension non reconnue : {p.suffix}")
        if not p.is_file():
            raise invalid(f"Fichier introuvable : {path}", open_errors=["swFileNotFoundError"])
        head = p.read_bytes()[:16]
        if not head or head.startswith(b"CORRUPT"):
            raise invalid(
                f"SolidWorks n'a pas pu ouvrir le fichier (corrompu ou illisible) : {p.name}",
                hint="Vérifier le fichier ; il est peut-être corrompu ou d'une version plus récente.",
                open_errors=["swGenericError"],
            )
        self._doc_seq += 1
        if path in self.saved:
            d = copy.deepcopy(self.saved[path])
            d.id = f"doc{self._doc_seq}"
        else:
            d = FDoc(id=f"doc{self._doc_seq}", name=p.name, type=doc_type)
        d.name, d.path, d.read_only, d.dirty, d.open_sketch = p.name, path, read_only, False, None
        self._add_doc(d)
        self._rebuild(d)
        return DocOpenResult(**self._ref(d).model_dump())

    def _find_doc(self, doc: str) -> FDoc:
        for d in self.docs:
            if doc in (d.id, d.name, d.path):
                return d
        raise invalid(
            f"Document introuvable : {doc[:80]}", hint="Appeler doc_list_open pour la liste des documents."
        )

    def activate_doc(self, doc: str) -> DocRef:
        self._tick()
        self.active = self._find_doc(doc)
        return self._ref(self.active)

    def _write(self, d: FDoc, path: str) -> None:
        Path(path).write_bytes(MAGIC + f" {d.type} volume_mm3={d.volume:.3f}\n".encode())
        snap = copy.deepcopy(d)
        snap.path, snap.dirty, snap.open_sketch = path, False, None
        self.saved[path] = snap

    def save_doc(self, path: str | None) -> str:
        d = self._doc()
        if d.open_sketch:
            raise SwError(ErrorCode.SKETCH_OPEN, "Une esquisse est ouverte.")
        target = path or d.path
        if target is None:
            raise invalid("Ce document n'a jamais été enregistré : `path` est obligatoire.")
        if Path(target).suffix.lower() != TYPE_EXT[d.type]:
            raise invalid(f"L'extension doit être {TYPE_EXT[d.type]} pour un document de type {d.type}.")
        if d.read_only and target == d.path:
            raise SwError(ErrorCode.READ_ONLY_MODE, "Document ouvert en lecture seule.")
        d.path, d.name, d.dirty = target, Path(target).name, False
        if path:
            d.read_only = False
        self._write(d, target)
        return target

    def save_copy(self, path: str) -> None:
        self._write(self._doc(), path)

    def close_doc(self, doc: str | None, save: bool) -> str:
        self._tick()
        d = self._find_doc(doc) if doc else self._doc()
        if save:
            prev, self.active = self.active, d
            try:
                self.save_doc(None)
            finally:
                self.active = prev
        self.docs.remove(d)
        self.registry.invalidate_doc(d.id)
        if self.active is d:
            self.active = self.docs[-1] if self.docs else None
        return d.name

    def doc_info(self) -> DocInfo:
        d = self._doc()
        return DocInfo(**self._ref(d).model_dump(), material=d.material, configuration="Défaut")

    def get_props(self) -> dict[str, str]:
        return dict(self._doc().props)

    def set_prop(self, name: str, value: str | None) -> PropChange:
        d = self._doc()
        before = d.props.get(name)
        if value is None:
            d.props.pop(name, None)
        else:
            d.props[name] = value
        d.dirty = True
        return PropChange(name=name, before=before, after=value)

    # --- noms et recherche de fonctions -------------------------------------------------------
    def _feat(self, d: FDoc, name: str) -> FFeature:
        for f in d.features:
            if f.name == name:
                return f
        raise invalid(
            f"Fonction ou esquisse introuvable : {name[:80]}",
            hint="Appeler inspect_tree pour les noms exacts.",
        )

    def _feat_by_uid(self, d: FDoc, uid: int | None) -> FFeature | None:
        return next((f for f in d.features if f.uid == uid), None)

    def _name(self, d: FDoc, prefix: str, wanted: str | None) -> str:
        names = {f.name for f in d.features} | set(PLANE_NAMES.values())
        if wanted:
            if wanted in names:
                raise invalid(f"Le nom '{wanted}' est déjà utilisé dans ce document.")
            return wanted
        n = 1
        while f"{prefix}_{n}" in names:
            n += 1
        return f"{prefix}_{n}"

    def _new_feature(self, d: FDoc, prefix: str, ftype: str, name: str | None, **params: Any) -> FFeature:
        f = FFeature(uid=d.next_uid, name=self._name(d, prefix, name), type=ftype, params=params)
        d.next_uid += 1
        return f

    def _no_sketch_open(self, d: FDoc) -> None:
        if d.open_sketch:
            sk = self._feat_by_uid(d, d.open_sketch)
            raise SwError(ErrorCode.SKETCH_OPEN, f"L'esquisse '{sk.name if sk else '?'}' est encore ouverte.")

    # --- esquisse --------------------------------------------------------------------------
    def _open(self) -> tuple[FDoc, FFeature, FSketch]:
        d = self._doc(part=True)
        f = self._feat_by_uid(d, d.open_sketch) if d.open_sketch else None
        if f is None or f.sketch is None:
            raise invalid(
                "Aucune esquisse n'est ouverte.", hint="Appeler sketch_begin avant d'ajouter des entités."
            )
        return d, f, f.sketch

    def _face_frame(self, d: FDoc, key: Key) -> SketchFrame | None:
        face = d.faces.get(key)
        if face is None or face.normal is None:
            return None
        return g.frame_for_plane(face.normal, face.centroid)

    def sketch_begin(self, plane: str, name: str | None) -> SketchBeginResult:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        face_key: Key | None = None
        if plane in g.PLANE_NORMALS:
            frame = g.frame_for_plane(g.PLANE_NORMALS[plane])
        else:
            face_key = self._face_key(d, plane)
            got = self._face_frame(d, face_key)
            if got is None:
                raise SwError(ErrorCode.SELECTION_FAILED, "Une esquisse exige un plan ou une face plane.")
            frame = got
            plane = "face"
        f = self._new_feature(d, "Sketch", "Sketch", name)
        f.sketch = FSketch(uid=f.uid, plane=plane, face_key=face_key, frame=frame)
        d.features.append(f)
        d.open_sketch = f.uid
        d.dirty = True
        return SketchBeginResult(name=f.name, plane=plane, frame=self._frame_out(frame))

    @staticmethod
    def _frame_out(f: SketchFrame) -> SketchFrame:
        return SketchFrame(
            origin=rnd3(f.origin), x_axis=rnd3(f.x_axis), y_axis=rnd3(f.y_axis), normal=rnd3(f.normal)
        )

    def _h(self, d: FDoc, sk: FSketch, local_id: int) -> str:
        return self.registry.register("sk", d.id, (sk.uid, local_id))

    def _pt(self, sk: FSketch, x: float, y: float) -> int:
        p = FPoint(sk.next_id, x, y)
        sk.points[p.id] = p
        sk.next_id += 1
        return p.id

    def _seg(self, sk: FSketch, type_: str, **kw: Any) -> FSeg:
        s = FSeg(sk.next_id, type_, **kw)
        sk.segs[s.id] = s
        sk.next_id += 1
        return s

    def _refs(
        self, d: FDoc, sk: FSketch, items: Sequence[tuple[int, str, str | None]]
    ) -> list[SketchEntityRef]:
        d.dirty = True
        return [SketchEntityRef(handle=self._h(d, sk, i), type=t, role=r) for i, t, r in items]

    def sketch_line(
        self, x1: float, y1: float, x2: float, y2: float, construction: bool
    ) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        a, b = self._pt(sk, x1, y1), self._pt(sk, x2, y2)
        s = self._seg(sk, "line", a=a, b=b, construction=construction)
        return self._refs(d, sk, [(s.id, "line", None), (a, "point", "start"), (b, "point", "end")])

    def sketch_polyline(self, points: list[Pt2], closed: bool) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        ids = [self._pt(sk, x, y) for x, y in points]
        out: list[tuple[int, str, str | None]] = []
        n = len(ids)
        for i in range(n if closed else n - 1):
            s = self._seg(sk, "line", a=ids[i], b=ids[(i + 1) % n])
            out.append((s.id, "line", f"segment_{i + 1}"))
        out += [(pid, "point", f"point_{i + 1}") for i, pid in enumerate(ids)]
        return self._refs(d, sk, out)

    def sketch_rect(self, x1: float, y1: float, x2: float, y2: float) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        xa, xb, ya, yb = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
        ids = [self._pt(sk, x, y) for x, y in ((xa, ya), (xb, ya), (xb, yb), (xa, yb))]
        roles = ["bottom", "right", "top", "left"]
        out: list[tuple[int, str, str | None]] = []
        for i, role in enumerate(roles):
            s = self._seg(sk, "line", a=ids[i], b=ids[(i + 1) % 4])
            sk.relations.append(("horizontal" if i % 2 == 0 else "vertical", [s.id]))
            sk.cost += 1
            out.append((s.id, "line", role))
        corners = ["bottom_left", "bottom_right", "top_right", "top_left"]
        out += [(pid, "point", corners[i]) for i, pid in enumerate(ids)]
        return self._refs(d, sk, out)

    def sketch_circle(self, cx: float, cy: float, radius: float) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        c = self._pt(sk, cx, cy)
        s = self._seg(sk, "circle", c=c, radius=radius)
        return self._refs(d, sk, [(s.id, "circle", None), (c, "point", "center")])

    def sketch_arc(
        self, cx: float, cy: float, x1: float, y1: float, x2: float, y2: float, ccw: bool
    ) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        r = math.hypot(x1 - cx, y1 - cy)
        if abs(r - math.hypot(x2 - cx, y2 - cy)) > 1e-3:
            raise invalid("Les deux extrémités de l'arc ne sont pas à la même distance du centre.")
        c, a, b = self._pt(sk, cx, cy), self._pt(sk, x1, y1), self._pt(sk, x2, y2)
        s = self._seg(sk, "arc", a=a, b=b, c=c, radius=r, ccw=ccw)
        return self._refs(
            d, sk, [(s.id, "arc", None), (a, "point", "start"), (b, "point", "end"), (c, "point", "center")]
        )

    def sketch_slot(self, x1: float, y1: float, x2: float, y2: float, width: float) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        length = math.hypot(x2 - x1, y2 - y1)
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        nx, ny, r = -uy * width / 2, ux * width / 2, width / 2
        c1, c2 = self._pt(sk, x1, y1), self._pt(sk, x2, y2)
        p = [
            self._pt(sk, *q)
            for q in ((x1 - nx, y1 - ny), (x2 - nx, y2 - ny), (x2 + nx, y2 + ny), (x1 + nx, y1 + ny))
        ]
        l1 = self._seg(sk, "line", a=p[0], b=p[1])
        a1 = self._seg(sk, "arc", a=p[1], b=p[2], c=c2, radius=r, ccw=True)
        l2 = self._seg(sk, "line", a=p[2], b=p[3])
        a2 = self._seg(sk, "arc", a=p[3], b=p[0], c=c1, radius=r, ccw=True)
        sk.relations += [
            ("tangent", [l1.id, a1.id]),
            ("tangent", [a1.id, l2.id]),
            ("tangent", [l2.id, a2.id]),
        ]
        sk.relations += [("tangent", [a2.id, l1.id]), ("equal", [a1.id, a2.id])]
        sk.cost += 5
        return self._refs(
            d,
            sk,
            [
                (l1.id, "line", "side_1"),
                (a1.id, "arc", "end_arc"),
                (l2.id, "line", "side_2"),
                (a2.id, "arc", "start_arc"),
                (c1, "point", "start_center"),
                (c2, "point", "end_center"),
            ],
        )

    def sketch_point(self, x: float, y: float) -> list[SketchEntityRef]:
        d, _, sk = self._open()
        pid = self._pt(sk, x, y)
        s = self._seg(sk, "point", a=pid)
        return self._refs(d, sk, [(s.id, "point", None)])

    def _ent(self, d: FDoc, sk: FSketch, handle: str) -> FPoint | FSeg | None:
        """Résout une entité de l'esquisse ouverte ; None désigne l'origine."""
        if handle == "origin":
            return None
        ref = self.registry.resolve(handle, "sk", d.id).ref
        assert isinstance(ref, tuple)
        uid, local = int(ref[0]), int(ref[1])
        if uid != sk.uid:
            raise invalid(f"Le handle {handle} n'appartient pas à l'esquisse ouverte.")
        local = sk.alias.get(local, local)
        if local in sk.points:
            return sk.points[local]
        seg = sk.segs.get(local)
        if seg is None:
            raise stale(handle)
        return sk.points[seg.a] if seg.type == "point" and seg.a else seg

    def _move(self, sk: FSketch, pid: int, x: float, y: float, seen: set[int] | None = None) -> None:
        seen = seen if seen is not None else set()
        p = sk.points[pid]
        if pid in seen or (p.fixed and seen):
            return
        seen.add(pid)
        p.x, p.y = x, y
        for kind, ents in sk.relations:
            if kind not in ("horizontal", "vertical") or len(ents) != 1:
                continue
            s = sk.segs.get(ents[0])
            if s is None or s.type != "line" or pid not in (s.a, s.b):
                continue
            other = s.b if s.a == pid else s.a
            assert other is not None
            o = sk.points[other]
            self._move(
                sk, other, o.x if kind == "horizontal" else x, y if kind == "horizontal" else o.y, seen
            )

    def _status(self, sk: FSketch) -> SketchStatus:
        dof = 2 * sum(1 for p in sk.points.values() if not p.fixed)
        dof += sum(1 for s in sk.segs.values() if s.type == "circle") - sum(
            1 for s in sk.segs.values() if s.type == "arc"
        )
        dof -= sk.cost + len(sk.dims)
        return "under_defined" if dof > 0 else "fully_defined" if dof == 0 else "over_defined"

    def sketch_relation(self, rel_type: str, entities: list[str]) -> RelationResult:
        d, _, sk = self._open()
        ents = [self._ent(d, sk, h) for h in entities]
        if rel_type in ("horizontal", "vertical"):
            if len(ents) == 2 and all(isinstance(e, FPoint) for e in ents):
                p0, p1 = ents
                assert isinstance(p0, FPoint) and isinstance(p1, FPoint)
                self._move(
                    sk,
                    p1.id,
                    p1.x if rel_type == "horizontal" else p0.x,
                    p0.y if rel_type == "horizontal" else p1.y,
                )
                sk.relations.append((rel_type, [p0.id, p1.id]))
                sk.cost += 1
            else:
                for e in ents:
                    if not isinstance(e, FSeg) or e.type != "line":
                        raise invalid(f"La relation {rel_type} s'applique à des lignes ou à deux points.")
                    assert e.a is not None and e.b is not None
                    a, b = sk.points[e.a], sk.points[e.b]
                    sk.relations.append((rel_type, [e.id]))
                    sk.cost += 1
                    self._move(
                        sk,
                        e.b,
                        b.x if rel_type == "horizontal" else a.x,
                        a.y if rel_type == "horizontal" else b.y,
                    )
        elif rel_type == "fix":
            for e in ents:
                if e is None:
                    continue
                pids = [e.id] if isinstance(e, FPoint) else [i for i in (e.a, e.b, e.c) if i]
                for pid in pids:
                    sk.points[pid].fixed = True
                if isinstance(e, FSeg) and e.type == "circle":
                    sk.cost += 1
                sk.relations.append(("fix", [e.id]))
        elif rel_type == "coincident":
            if len(ents) != 2:
                raise invalid("La relation coincident exige exactement deux entités.")
            pts = [e for e in ents if isinstance(e, FPoint)]
            if not pts:
                raise invalid("La relation coincident exige au moins un point (ou 'origin').")
            other = next((e for e in ents if e is not pts[0]), None) if len(pts) == 1 else pts[1]
            p = pts[0]
            if None in ents:
                self._move(sk, p.id, 0.0, 0.0)
                p.fixed = True
                sk.relations.append(("coincident", [p.id, "origin"]))
            elif isinstance(other, FPoint):
                self._move(sk, other.id, p.x, p.y)
                for s in sk.segs.values():
                    s.a, s.b, s.c = (p.id if i == other.id else i for i in (s.a, s.b, s.c))
                p.fixed = p.fixed or other.fixed
                del sk.points[other.id]
                sk.alias[other.id] = p.id
                sk.relations.append(("coincident", [p.id, p.id]))
            else:
                assert isinstance(other, FSeg)
                sk.relations.append(("coincident", [p.id, other.id]))
                sk.cost += 1
        else:
            need = REL_MIN.get(rel_type, 2)
            if len(ents) < need or any(e is None for e in ents):
                raise invalid(f"La relation {rel_type} exige au moins {need} entités de l'esquisse.")
            sk.relations.append((rel_type, [e.id for e in ents if e is not None]))
            sk.cost += REL_COST[rel_type]
        d.dirty = True
        return RelationResult(relation=rel_type, entities=entities, status=self._status(sk))

    @staticmethod
    def _xy(sk: FSketch, e: FPoint | FSeg | None) -> Pt2:
        if e is None:
            return (0.0, 0.0)
        if isinstance(e, FPoint):
            return (e.x, e.y)
        pid = e.c if e.type in ("circle", "arc") else e.a
        assert pid is not None
        return (sk.points[pid].x, sk.points[pid].y)

    def _apply_dim(self, sk: FSketch, dim: FDim) -> None:
        def ent(i: Any) -> FPoint | FSeg | None:
            if i == "origin":
                return None
            return sk.points.get(i) or sk.segs.get(i) if isinstance(i, int) else None

        ents = [ent(i) for i in dim.ents]
        v = dim.value
        e0 = ents[0]
        if dim.kind in ("diameter", "radius"):
            assert isinstance(e0, FSeg) and e0.c is not None
            e0.radius = v / 2 if dim.kind == "diameter" else v
            c = sk.points[e0.c]
            for pid in (e0.a, e0.b):
                if pid:
                    p = sk.points[pid]
                    ang = math.atan2(p.y - c.y, p.x - c.x)
                    p.x, p.y = c.x + e0.radius * math.cos(ang), c.y + e0.radius * math.sin(ang)
        elif dim.kind == "length":
            assert isinstance(e0, FSeg) and e0.a is not None and e0.b is not None
            a, b = sk.points[e0.a], sk.points[e0.b]
            length = math.hypot(b.x - a.x, b.y - a.y) or 1.0
            ux, uy = (b.x - a.x) / length, (b.y - a.y) / length
            self._move(sk, b.id, a.x + ux * v, a.y + uy * v)
        elif dim.kind == "angle":
            e1 = ents[1]
            assert isinstance(e0, FSeg) and isinstance(e1, FSeg) and None not in (e0.a, e0.b, e1.a, e1.b)
            a0, b0, a1, b1 = (sk.points[i] for i in (e0.a, e0.b, e1.a, e1.b))  # type: ignore[index]
            base = math.atan2(b0.y - a0.y, b0.x - a0.x)
            cur = math.atan2(b1.y - a1.y, b1.x - a1.x)
            sign = 1.0 if math.sin(cur - base) >= 0 else -1.0
            length = math.hypot(b1.x - a1.x, b1.y - a1.y)
            ang = base + sign * math.radians(v)
            self._move(sk, b1.id, a1.x + length * math.cos(ang), a1.y + length * math.sin(ang))
        else:  # distance
            e1 = ents[1]
            line0 = e0 if isinstance(e0, FSeg) and e0.type == "line" else None
            line1 = e1 if isinstance(e1, FSeg) and e1.type == "line" else None
            p0, p1 = self._xy(sk, e0), self._xy(sk, e1)
            ref_line = line0 or line1
            if ref_line is not None:
                assert ref_line.a is not None and ref_line.b is not None
                a, b = sk.points[ref_line.a], sk.points[ref_line.b]
                length = math.hypot(b.x - a.x, b.y - a.y) or 1.0
                nx, ny = -(b.y - a.y) / length, (b.x - a.x) / length
            else:
                dd = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
                nx, ny = ((p1[0] - p0[0]) / dd, (p1[1] - p0[1]) / dd) if dd > 1e-9 else (1.0, 0.0)
            cur = (p1[0] - p0[0]) * nx + (p1[1] - p0[1]) * ny
            shift = (v if cur >= 0 else -v) - cur
            if isinstance(e1, FPoint):
                moved = [e1.id]
            elif isinstance(e1, FSeg):
                moved = [i for i in ((e1.a, e1.b) if e1.type == "line" else (e1.c,)) if i]
            else:
                raise invalid("La seconde entité d'une cote de distance ne peut pas être l'origine.")
            targets = [(pid, sk.points[pid].x + nx * shift, sk.points[pid].y + ny * shift) for pid in moved]
            seen: set[int] = set()
            for pid, x, y in targets:
                seen.discard(pid)
                self._move(sk, pid, x, y, seen if len(targets) > 1 else None)

    def sketch_dimension(
        self, dim_type: str, entities: list[str], value: float, name: str | None
    ) -> DimensionResult:
        d, f, sk = self._open()
        ents = [self._ent(d, sk, h) for h in entities]
        need = {"length": 1, "diameter": 1, "radius": 1, "angle": 2, "distance": 2}[dim_type]
        if len(ents) != need:
            raise invalid(f"Une cote '{dim_type}' exige {need} entité(s), {len(ents)} fournie(s).")
        e0 = ents[0]
        if dim_type in ("diameter", "radius") and not (isinstance(e0, FSeg) and e0.type in ("circle", "arc")):
            raise invalid(f"Une cote '{dim_type}' s'applique à un cercle ou à un arc.")
        if dim_type == "length" and not (isinstance(e0, FSeg) and e0.type == "line"):
            raise invalid("Une cote 'length' s'applique à une ligne.")
        if dim_type == "angle" and not all(isinstance(e, FSeg) and e.type == "line" for e in ents):
            raise invalid("Une cote 'angle' s'applique à deux lignes.")
        dname = name or f"D{len(sk.dims) + 1}"
        if any(x.name == dname for x in sk.dims):
            raise invalid(f"La cote '{dname}' existe déjà dans l'esquisse {f.name}.")
        ids = ["origin" if e is None else e.id for e in ents]
        dim = FDim(dname, dim_type, ids, value, "deg" if dim_type == "angle" else "mm")
        self._apply_dim(sk, dim)
        sk.dims.append(dim)
        d.dirty = True
        return DimensionResult(
            full_name=f"{dname}@{f.name}", value=value, unit=dim.unit, status=self._status(sk)
        )

    def _segments(self, d: FDoc, sk: FSketch) -> list[SketchSegment]:
        out = []
        for s in sk.segs.values():

            def xy(pid: int | None) -> Pt2 | None:
                return (rnd(sk.points[pid].x), rnd(sk.points[pid].y)) if pid else None

            if s.type == "point":
                out.append(SketchSegment(handle=self._h(d, sk, s.id), type="point", start=xy(s.a)))
                continue
            out.append(
                SketchSegment(
                    handle=self._h(d, sk, s.id),
                    type=s.type,
                    start=xy(s.a),
                    end=xy(s.b),
                    center=xy(s.c),
                    radius_mm=s.radius or None,
                    ccw=s.ccw,
                    construction=s.construction,
                )
            )
        return out

    def sketch_end(self) -> SketchEndResult:
        d, f, sk = self._open()
        d.open_sketch = None
        if not sk.segs:
            d.features.remove(f)
            return SketchEndResult(
                name=f.name,
                status="failed",
                closed=False,
                segment_count=0,
                issues=["esquisse vide : supprimée"],
            )
        rep = g.analyze_profile(self._segments(d, sk))
        return SketchEndResult(
            name=f.name,
            status=self._status(sk),
            closed=rep.usable,
            segment_count=len(sk.segs),
            issues=rep.issues[:10],
        )

    def sketch_inspect(self, name: str | None) -> SketchInspectResult:
        d = self._doc(part=True)
        if name is None:
            _, f, sk = self._open()
        else:
            f = self._feat(d, name)
            if f.sketch is None:
                raise invalid(f"'{name}' n'est pas une esquisse.")
            sk = f.sketch
        segs = self._segments(d, sk)
        rep = g.analyze_profile(segs)

        def hs(ids: list[Any]) -> list[str]:
            return ["origin" if i == "origin" else self._h(d, sk, i) for i in ids]

        return SketchInspectResult(
            name=f.name,
            status=self._status(sk),
            closed=rep.usable,
            frame=self._frame_out(sk.frame),
            segments=segs,
            relations=[RelationInfo(type=k, entities=hs(e)) for k, e in sk.relations],
            dimensions=[
                DimInfo(full_name=f"{x.name}@{f.name}", value=rnd(x.value), unit=x.unit, feature=f.name)
                for x in sk.dims
            ],
            issues=rep.issues[:10],
        )

    # --- reconstruction ----------------------------------------------------------------------
    def _profile(self, d: FDoc, sketch_name: str) -> tuple[FSketch, g.ProfileReport]:
        sf = next((x for x in d.features if x.name == sketch_name and x.sketch), None)
        if sf is None or sf.sketch is None:
            raise invalid(f"Esquisse introuvable : {sketch_name[:80]}")
        sk = sf.sketch
        if sk.face_key is not None:
            frame = self._face_frame(d, sk.face_key)
            if frame is None:
                raise SwError(
                    ErrorCode.REBUILD_FAILED, f"Référence pendante : la face support de {sf.name} a disparu."
                )
            sk.frame = frame
        rep = g.analyze_profile(self._segments(d, sk))
        if not rep.usable:
            raise SwError(
                ErrorCode.SKETCH_NOT_CLOSED,
                f"Le contour de l'esquisse {sf.name} n'est pas exploitable : " + "; ".join(rep.issues[:3]),
                hint=f"Corriger l'esquisse {sf.name} (sketch_inspect) : contour fermé, sans segments sécants ni dupliqués.",
                details={"sketch": sf.name, "closed": rep.closed, "issues": rep.issues[:5]},
            )
        return sk, rep

    def _apply(self, d: FDoc, f: FFeature, sign: str, prim: fs.Prim, merge: bool = True) -> float:
        vol = fs.op_volume(d.ops, sign, prim)
        if sign == "-" and vol < 1e-9:
            raise SwError(ErrorCode.FEATURE_FAILED, "L'enlèvement de matière ne rencontre aucune matière.")
        if sign == "+":
            d.bodies = 1 if (merge or not d.ops) and d.bodies <= 1 else d.bodies + 1
            d.bbox_pts += prim.bbox_points()
        d.ops.append((sign, prim))
        delta = vol if sign == "+" else -vol
        d.volume += delta
        f.delta += delta
        d.moment = g.add(d.moment, g.scale(prim.centroid, delta))
        return vol

    def _extent(self, d: FDoc, frame: SketchFrame) -> tuple[float, float]:
        ws = [g.to_sketch(frame, p)[2] for p in d.bbox_pts]
        return (min(ws) - 1.0, max(ws) + 1.0)

    def _build_extrude(self, d: FDoc, f: FFeature) -> None:
        p = f.params
        sk, rep = self._profile(d, p["sketch"])
        cut = p["cut"]
        sgn = (-1.0 if cut else 1.0) * (-1.0 if p["direction"] == "reverse" else 1.0)
        both = p["direction"] == "both"
        end = p["end"]
        depth = f.dims.get("D1", 0.0)
        if end in ("through_all", "up_to_next"):
            if not d.ops:
                raise SwError(ErrorCode.FEATURE_FAILED, f"La condition {end} exige un solide existant.")
            lo, hi = self._extent(d, sk.frame)
            if end == "up_to_next":
                c = rep.centroid
                o = g.to_model(sk.frame, c[0], c[1])
                mat = fs.material(d.ops, o, g.scale(sk.frame.normal, sgn), 0.0, max(abs(lo), abs(hi)))
                depth = (mat[0][1] if cut else mat[0][0]) if mat else 0.0
                if depth < 1e-9:
                    raise SwError(
                        ErrorCode.FEATURE_FAILED, "Aucune face suivante trouvée dans cette direction."
                    )
                w0, w1 = (0.0, depth) if sgn > 0 else (-depth, 0.0)
            else:
                w0, w1 = (lo, hi) if both else ((0.0, hi) if sgn > 0 else (lo, 0.0))
        elif end == "mid_plane":
            w0, w1 = -depth / 2, depth / 2
        else:
            w0, w1 = (-depth, depth) if both else ((0.0, depth) if sgn > 0 else (-depth, 0.0))
        prim = fs.Prism(sk.frame, rep, w0, w1)
        vol = self._apply(d, f, "-" if cut else "+", prim, p.get("merge", True))
        height = vol / rep.area if cut else (w1 - w0)
        self._prism_topology(d, f, sk, prim, cut, height, sgn)
        if cut and sk.face_key in d.faces:
            d.faces[sk.face_key].area = max(0.0, d.faces[sk.face_key].area - rep.area)

    def _prism_topology(
        self, d: FDoc, f: FFeature, sk: FSketch, prim: fs.Prism, cut: bool, height: float, sgn: float
    ) -> None:
        fr, rep = prim.frame, prim.profile
        if cut:  # parois : de l'entrée (plan d'esquisse) sur la hauteur de matière traversée
            wa, wb = (0.0, height) if sgn > 0 else (-height, 0.0)
            if prim.w0 < 0 < prim.w1:
                wa, wb = -height / 2, height / 2
        else:
            wa, wb = prim.w0, prim.w1
        wm = (wa + wb) / 2
        c = rep.centroid
        caps: list[tuple[str, float, Vec3]] = []
        if not cut:
            caps = [("cap0", wa, g.scale(fr.normal, -1.0)), ("cap1", wb, fr.normal)]
        else:
            w_end = wb if sgn > 0 else wa
            beyond = g.to_model(fr, c[0], c[1], w_end + sgn * 1e-4)
            if fs.is_inside(d.ops[:-1], beyond):  # fond borgne
                caps = [("cap1", w_end, g.scale(fr.normal, -sgn))]
        for role, w, n in caps:
            d.faces[(f.uid, role)] = FFace(
                (f.uid, role), "plane", rep.area, g.to_model(fr, c[0], c[1], w), n, None, f.uid
            )
        seg_of = {s.handle: s for lp in rep.loops for s in lp.segments}
        ids = {self._h(d, sk, s.id): s.id for s in sk.segs.values()}
        vertices: dict[tuple[int, int], list[Key]] = {}
        for handle, s in seg_of.items():
            sid = ids[handle]
            skey: Key = (f.uid, "side", sid)
            pts2 = g.discretize(s, 24)
            length = g.seg_length(s)
            if s.type == "line":
                assert s.start is not None and s.end is not None
                mx, my = (s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2
                px, py = -(s.end[1] - s.start[1]) / length, (s.end[0] - s.start[0]) / length
                if rep.contains((mx + px * 1e-4, my + py * 1e-4)):
                    px, py = -px, -py
                if cut:
                    px, py = -px, -py
                normal: Vec3 | None = g.add(g.scale(fr.x_axis, px), g.scale(fr.y_axis, py))
                d.faces[skey] = FFace(
                    skey, "plane", length * height, g.to_model(fr, mx, my, wm), normal, None, f.uid
                )
            else:
                mid = pts2[len(pts2) // 2] if s.type == "arc" else s.center
                assert mid is not None
                d.faces[skey] = FFace(
                    skey,
                    "cylinder",
                    length * height,
                    g.to_model(fr, mid[0], mid[1], wm),
                    None,
                    s.radius_mm,
                    f.uid,
                )
            for role, w in (("e0", wa), ("e1", wb)):
                cap_key: Key = (f.uid, "cap0" if role == "e0" else "cap1")
                adj = [skey] + ([cap_key] if cap_key in d.faces else [])
                if cut and sk.face_key in d.faces and abs(w) < 1e-9:
                    adj.append(sk.face_key)
                center = g.to_model(fr, s.center[0], s.center[1], w) if s.center else None
                d.edges[(f.uid, role, sid)] = FEdge(
                    (f.uid, role, sid),
                    {"line": "line", "circle": "circle", "arc": "arc"}[s.type],
                    length,
                    [g.to_model(fr, x, y, w) for x, y in pts2],
                    center,
                    s.radius_mm,
                    adj,
                    f.uid,
                )
            if s.type != "circle":
                for end_pt in (s.start, s.end):
                    assert end_pt is not None
                    vertices.setdefault(g._key(end_pt), []).append(skey)
                    vertices[g._key(end_pt)].append(("pt", end_pt))
        for i, (_, lst) in enumerate(sorted(vertices.items())):
            pt = next(x[1] for x in lst if x[0] == "pt")
            adj = [x for x in lst if x[0] != "pt"]
            pts = [g.to_model(fr, pt[0], pt[1], wa), g.to_model(fr, pt[0], pt[1], wb)]
            d.edges[(f.uid, "v", i)] = FEdge((f.uid, "v", i), "line", height, pts, None, None, adj, f.uid)

    def _build_revolve(self, d: FDoc, f: FFeature) -> None:
        p = f.params
        sk, rep = self._profile(d, p["sketch"])
        ax = sk.segs.get(p["axis"])
        if ax is None or ax.type != "line" or ax.a is None or ax.b is None:
            raise SwError(ErrorCode.REBUILD_FAILED, "Référence pendante : l'axe de révolution a disparu.")
        a, b = sk.points[ax.a], sk.points[ax.b]
        length = math.hypot(b.x - a.x, b.y - a.y)
        prim = fs.Revolve(
            sk.frame,
            rep,
            (a.x, a.y),
            ((b.x - a.x) / length, (b.y - a.y) / length),
            math.radians(f.dims["D1"]),
        )
        sides = {
            (prim._split(q)[1] > 1e-7) - (prim._split(q)[1] < -1e-7) for lp in rep.loops for q in lp.polygon
        }
        if {1, -1} <= sides:
            raise SwError(ErrorCode.FEATURE_FAILED, "Le contour traverse l'axe de révolution.")
        self._apply(d, f, "-" if p["cut"] else "+", prim)
        axis3 = g.add(g.scale(sk.frame.x_axis, prim.axis_dir[0]), g.scale(sk.frame.y_axis, prim.axis_dir[1]))
        full = prim.angle >= 2 * math.pi - 1e-9
        n = 0
        for lp in rep.loops:
            for s in lp.segments:
                pts2 = g.discretize(s, 24)
                mid = pts2[len(pts2) // 2]
                axial, rad = prim._split(mid)
                rad = abs(rad)
                on_axis = g.to_model(
                    sk.frame,
                    prim.axis_point[0] + axial * prim.axis_dir[0],
                    prim.axis_point[1] + axial * prim.axis_dir[1],
                )
                surface, normal, radius = "other", None, None
                if s.type == "line":
                    assert s.start is not None and s.end is not None
                    da = abs(prim._split(s.end)[0] - prim._split(s.start)[0])
                    dr = abs(abs(prim._split(s.end)[1]) - abs(prim._split(s.start)[1]))
                    if dr < 1e-7:
                        surface, radius = "cylinder", rad
                    elif da < 1e-7:
                        surface = "plane"
                        probe = (mid[0] + prim.axis_dir[0] * 1e-4, mid[1] + prim.axis_dir[1] * 1e-4)
                        normal = g.scale(axis3, -1.0 if rep.contains(probe) else 1.0)
                    else:
                        surface = "cone"
                area = g.seg_length(s) * rad * prim.angle
                if area < 1e-9:
                    continue
                key: Key = (f.uid, "rev", n)
                centroid = on_axis if full else g.to_model(sk.frame, mid[0], mid[1])
                d.faces[key] = FFace(key, surface, area, centroid, normal, radius, f.uid)
                n += 1
        seen: set[tuple[int, int]] = set()
        for lp in rep.loops:
            for q in lp.polygon if all(s.type == "line" for s in lp.segments) else []:
                axial, rad = prim._split(q)
                k = g._key((axial, abs(rad)))
                if abs(rad) < 1e-7 or k in seen:
                    continue
                seen.add(k)
                base = (
                    prim.axis_point[0] + axial * prim.axis_dir[0],
                    prim.axis_point[1] + axial * prim.axis_dir[1],
                )
                center = g.to_model(sk.frame, base[0], base[1])
                pts = []
                for i in range(25):
                    ang = prim.angle * i / 24
                    u = (
                        base[0] - rad * math.cos(ang) * prim.axis_dir[1],
                        base[1] + rad * math.cos(ang) * prim.axis_dir[0],
                    )
                    pts.append(g.to_model(sk.frame, u[0], u[1], rad * math.sin(ang)))
                ekey: Key = (f.uid, "rc", len(seen))
                d.edges[ekey] = FEdge(
                    ekey, "circle" if full else "arc", abs(rad) * prim.angle, pts, center, abs(rad), [], f.uid
                )

    def _edge_blend(
        self, d: FDoc, f: FFeature, size: float, section: float, width: float, surface: str
    ) -> None:
        total = 0.0
        for i, key in enumerate(f.params["edges"]):
            e = d.edges.get(key)
            if e is None:
                raise SwError(
                    ErrorCode.REBUILD_FAILED, f"Référence pendante : une arête de {f.name} a disparu."
                )
            for fk in e.faces:
                face = d.faces.get(fk)
                if (
                    face
                    and face.surface == "plane"
                    and e.length > 1e-9
                    and face.area / e.length < size - 1e-9
                ):
                    raise SwError(
                        ErrorCode.FEATURE_FAILED,
                        f"La valeur {size:g} mm dépasse la largeur disponible ({face.area / e.length:.3g} mm) "
                        "d'une face adjacente.",
                        hint="Réduire le rayon ou la distance : il doit rester inférieur à l'épaisseur locale.",
                    )
            total += section * e.length
            mid = e.pts[len(e.pts) // 2] if len(e.pts) > 2 else g.scale(g.add(e.pts[0], e.pts[-1]), 0.5)
            fkey: Key = (f.uid, "blend", i)
            d.faces[fkey] = FFace(
                fkey, surface, width * e.length, mid, None, size if surface == "cylinder" else None, f.uid
            )
            del d.edges[key]
        total = min(total, d.volume)
        d.volume -= total
        f.delta = -total

    def _build_fillet(self, d: FDoc, f: FFeature) -> None:
        r = f.dims["D1"]
        self._edge_blend(d, f, r, (1 - math.pi / 4) * r * r, math.pi * r / 2, "cylinder")

    def _build_chamfer(self, d: FDoc, f: FFeature) -> None:
        dist, ang = f.dims["D1"], math.radians(f.dims["D2"])
        other = dist * math.tan(ang)
        self._edge_blend(d, f, max(dist, other), dist * other / 2, math.hypot(dist, other), "plane")

    def _build_shell(self, d: FDoc, f: FFeature) -> None:
        t = f.dims["D1"]
        box = _bbox(d.bbox_pts)
        if box is None or d.volume <= 0:
            raise SwError(ErrorCode.FEATURE_FAILED, "La coque exige un solide existant.")
        if 2 * t >= min(box.size) - 1e-9:
            raise SwError(
                ErrorCode.FEATURE_FAILED,
                f"L'épaisseur {t:g} mm est trop grande pour la pièce (plus petite dimension {min(box.size):g} mm).",
                hint="Réduire thickness_mm à moins de la moitié de la plus petite dimension.",
            )
        inner = [s - 2 * t for s in box.size]
        for key in f.params["faces"]:
            face = d.faces.get(key)
            if face is None:
                raise SwError(
                    ErrorCode.REBUILD_FAILED, f"Référence pendante : une face de {f.name} a disparu."
                )
            if face.normal is not None:
                axis = max(range(3), key=lambda i: abs(face.normal[i]))  # type: ignore[index]
                inner[axis] += t
            del d.faces[key]
        fill = d.volume / (box.size[0] * box.size[1] * box.size[2])
        removed = inner[0] * inner[1] * inner[2] * fill
        d.volume -= removed
        f.delta = -removed

    def _build_pattern(self, d: FDoc, f: FFeature) -> None:
        p = f.params
        maps: list[fs.PointMap] = []
        if p["mode"] == "linear":
            maps = [fs.translation(g.scale(p["dir"], f.dims["D1"] * i)) for i in range(1, p["count"])]
        elif p["mode"] == "circular":
            step = math.radians(f.dims["D1"]) / (
                p["count"] if abs(f.dims["D1"] - 360) < 1e-9 else p["count"] - 1
            )
            maps = [fs.rotation(p["point"], p["dir"], step * i) for i in range(1, p["count"])]
        else:
            maps = [fs.mirror(p["point"], p["dir"])]
        for src_uid in p["sources"]:
            src = self._feat_by_uid(d, src_uid)
            if src is None:
                raise SwError(
                    ErrorCode.REBUILD_FAILED,
                    f"Référence pendante : une fonction source de {f.name} a disparu.",
                )
            if src.suppressed or src.error:
                continue
            prims = list(self._ops_of(d, src))
            faces = [x for x in d.faces.values() if x.feature == src.uid]
            edges = [x for x in d.edges.values() if x.feature == src.uid]
            for i, t in enumerate(maps):
                for sign, prim in prims:
                    self._apply(d, f, sign, prim.transformed(t))

                def vec(v: Vec3, t: fs.PointMap = t) -> Vec3:
                    return g.sub(t(v), t((0.0, 0.0, 0.0)))

                for face in faces:
                    k: Key = (f.uid, i, face.key)
                    d.faces[k] = FFace(
                        k,
                        face.surface,
                        face.area,
                        t(face.centroid),
                        vec(face.normal) if face.normal else None,
                        face.radius,
                        f.uid,
                    )
                for e in edges:
                    k = (f.uid, i, e.key)
                    d.edges[k] = FEdge(
                        k,
                        e.curve,
                        e.length,
                        [t(q) for q in e.pts],
                        t(e.center) if e.center else None,
                        e.radius,
                        [(f.uid, i, fk) for fk in e.faces],
                        f.uid,
                    )

    def _ops_of(self, d: FDoc, src: FFeature) -> list[fs.Op]:
        """Opérations élémentaires déjà appliquées par une fonction source."""
        lo, hi = d.op_index.get(src.uid, (0, 0))
        return d.ops[lo:hi]

    # --- fonctions -------------------------------------------------------------------------
    def _create(self, d: FDoc, f: FFeature) -> FeatureCreated:
        """Ajoute la fonction, reconstruit ; en cas d'échec elle est annulée (jamais d'état ambigu)."""
        d.features.append(f)
        self._rebuild(d)
        if f.error:
            d.features.remove(f)
            self._rebuild(d)
            sketch = f.params.get("sketch")
            details: dict[str, Any] = {"feature": f.name, "type": f.type, "sw_error": f.error, "kept": False}
            hint = f.hint
            if sketch:
                sf = next((x for x in d.features if x.name == sketch and x.sketch), None)
                if sf and sf.sketch:
                    rep = g.analyze_profile(self._segments(d, sf.sketch))
                    details["sketch"] = {
                        "name": sketch,
                        "closed": rep.usable,
                        "status": self._status(sf.sketch),
                    }
                    if not rep.usable:
                        raise SwError(
                            ErrorCode.SKETCH_NOT_CLOSED,
                            f.error,
                            hint=f"Le contour de l'esquisse {sketch} n'est pas fermé ou se recoupe : "
                            + "; ".join(rep.issues[:2]),
                            details=details,
                        )
            raise SwError(
                ErrorCode.FEATURE_FAILED, f"{f.type} '{f.name}' : {f.error}", hint=hint, details=details
            )
        d.dirty = True
        return FeatureCreated(
            feature=f.name,
            type=f.type,
            dimensions=[f"{k}@{f.name}" for k in f.dims],
            sketch=f.params.get("sketch"),
        )

    def _rebuild(self, d: FDoc) -> None:
        """Rejoue toutes les fonctions ; mémorise pour chacune la plage de ses opérations élémentaires."""
        d.op_index = {}
        d.ops, d.faces, d.edges = [], {}, {}
        d.volume, d.moment, d.bbox_pts, d.bodies = 0.0, (0.0, 0.0, 0.0), [], 0
        for f in d.features:
            f.error, f.delta = None, 0.0
            if f.suppressed or f.type == "Sketch":
                continue
            start = len(d.ops)
            snapshot = (
                list(d.ops),
                dict(d.faces),
                dict(d.edges),
                d.volume,
                d.moment,
                list(d.bbox_pts),
                d.bodies,
            )
            try:
                getattr(self, f"_build_{f.params['kind']}")(d, f)
            except SwError as exc:
                f.error, f.hint, f.delta = exc.message, exc.hint, 0.0
                d.ops, d.faces, d.edges, d.volume, d.moment, d.bbox_pts, d.bodies = snapshot
            d.op_index[f.uid] = (start, len(d.ops))

    def _consumed(self, d: FDoc, sketch: str) -> None:
        sf = self._feat(d, sketch)
        if sf.sketch is None:
            raise invalid(f"'{sketch}' n'est pas une esquisse.")

    def extrude(
        self,
        sketch: str,
        depth: float | None,
        end_condition: str,
        direction: str,
        merge: bool,
        draft_deg: float,
        name: str | None,
        cut: bool,
    ) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        self._consumed(d, sketch)
        f = self._new_feature(
            d,
            "Cut" if cut else "Extrude",
            "CutExtrude" if cut else "Extrude",
            name,
            kind="extrude",
            sketch=sketch,
            end=end_condition,
            direction=direction,
            merge=merge,
            cut=cut,
            draft=draft_deg,
        )
        if end_condition in ("blind", "mid_plane"):
            assert depth is not None
            f.dims["D1"], f.dim_units["D1"] = depth, "mm"
        return self._create(d, f)

    def revolve(
        self, sketch: str, axis: str, angle_deg: float, cut: bool, name: str | None
    ) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        self._consumed(d, sketch)
        sf = self._feat(d, sketch)
        assert sf.sketch is not None
        ref = self.registry.resolve(axis, "sk", d.id).ref
        assert isinstance(ref, tuple)
        seg = sf.sketch.segs.get(ref[1]) if ref[0] == sf.sketch.uid else None
        if seg is None or seg.type != "line":
            raise invalid(
                f"L'axe doit être une ligne de l'esquisse {sketch} (idéalement une ligne de construction)."
            )
        f = self._new_feature(
            d,
            "CutRevolve" if cut else "Revolve",
            "CutRevolve" if cut else "Revolve",
            name,
            kind="revolve",
            sketch=sketch,
            axis=seg.id,
            cut=cut,
        )
        f.dims["D1"], f.dim_units["D1"] = angle_deg, "deg"
        return self._create(d, f)

    def _edge_key(self, d: FDoc, handle: str) -> Key:
        key = self.registry.resolve(handle, "edge", d.id).ref
        if key not in d.edges:
            raise stale(handle)
        return key

    def _face_key(self, d: FDoc, handle: str) -> Key:
        key = self.registry.resolve(handle, "face", d.id).ref
        if key not in d.faces:
            raise stale(handle)
        return key

    def fillet(self, edges: list[str], radius: float, name: str | None) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        f = self._new_feature(
            d, "Fillet", "Fillet", name, kind="fillet", edges=[self._edge_key(d, h) for h in edges]
        )
        f.dims["D1"], f.dim_units["D1"] = radius, "mm"
        return self._create(d, f)

    def chamfer(
        self, edges: list[str], distance: float, angle_deg: float, name: str | None
    ) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        f = self._new_feature(
            d, "Chamfer", "Chamfer", name, kind="chamfer", edges=[self._edge_key(d, h) for h in edges]
        )
        f.dims.update(D1=distance, D2=angle_deg)
        f.dim_units.update(D1="mm", D2="deg")
        return self._create(d, f)

    def hole(
        self, face: str, points: list[Vec3], diameter: float, depth: float | None, name: str | None
    ) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        return compose_hole(self, face, points, diameter, depth, self._name(d, "Hole", name))

    def shell(self, faces_to_remove: list[str], thickness: float, name: str | None) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        f = self._new_feature(
            d, "Shell", "Shell", name, kind="shell", faces=[self._face_key(d, h) for h in faces_to_remove]
        )
        f.dims["D1"], f.dim_units["D1"] = thickness, "mm"
        return self._create(d, f)

    def _sources(self, d: FDoc, features: list[str]) -> list[int]:
        out = []
        for n in features:
            src = self._feat(d, n)
            if src.params.get("kind") not in ("extrude", "revolve"):
                raise invalid(
                    f"'{n}' ({src.type}) ne peut pas être répété : extrusion, enlèvement ou révolution attendus."
                )
            out.append(src.uid)
        return out

    def _direction(self, d: FDoc, text: str) -> tuple[Vec3, Vec3]:
        """(point, direction) d'un axe nommé, d'une arête ou d'une face cylindrique."""
        axis = g.parse_axis(text)
        if axis is not None:
            return (0.0, 0.0, 0.0), axis
        ref = self.registry.resolve(text, None, d.id)
        if ref.kind == "edge":
            e = d.edges.get(ref.ref)  # type: ignore[call-overload]
            if e is None:
                raise stale(text)
            if e.curve == "line":
                return e.pts[0], g.unit(g.sub(e.pts[-1], e.pts[0]))
            if e.center is not None and len(e.pts) >= 3:
                return e.center, g.unit(g.cross(g.sub(e.pts[0], e.center), g.sub(e.pts[1], e.center)))
        raise invalid("Direction ou axe invalide : 'x', 'y', 'z' ou handle d'une arête attendu.")

    def pattern_linear(
        self, features: list[str], direction: str, count: int, spacing: float, name: str | None
    ) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        _, vec = self._direction(d, direction)
        f = self._new_feature(
            d,
            "LPattern",
            "LPattern",
            name,
            kind="pattern",
            mode="linear",
            sources=self._sources(d, features),
            dir=vec,
            count=count,
        )
        f.dims["D1"], f.dim_units["D1"] = spacing, "mm"
        return self._create(d, f)

    def pattern_circular(
        self, features: list[str], axis: str, count: int, angle_deg: float, name: str | None
    ) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        point, vec = self._direction(d, axis)
        f = self._new_feature(
            d,
            "CPattern",
            "CPattern",
            name,
            kind="pattern",
            mode="circular",
            sources=self._sources(d, features),
            dir=vec,
            point=point,
            count=count,
        )
        f.dims["D1"], f.dim_units["D1"] = angle_deg, "deg"
        return self._create(d, f)

    def mirror(self, features: list[str], plane: str, name: str | None) -> FeatureCreated:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        if plane in g.PLANE_NORMALS:
            point, normal = (0.0, 0.0, 0.0), g.PLANE_NORMALS[plane]
        else:
            face = d.faces[self._face_key(d, plane)]
            if face.normal is None:
                raise SwError(ErrorCode.SELECTION_FAILED, "Le plan de symétrie doit être une face plane.")
            point, normal = face.centroid, face.normal
        f = self._new_feature(
            d,
            "Mirror",
            "Mirror",
            name,
            kind="pattern",
            mode="mirror",
            sources=self._sources(d, features),
            dir=normal,
            point=point,
            count=2,
        )
        return self._create(d, f)

    def _split_dim(self, d: FDoc, full_name: str) -> tuple[FFeature, str]:
        if full_name.count("@") != 1:
            raise invalid(
                f"Nom de cote invalide : {full_name[:80]}",
                hint="Format attendu : <cote>@<fonction>, ex. D1@Base_1.",
            )
        dname, fname = full_name.split("@")
        return self._feat(d, fname), dname

    def set_dimension(self, full_name: str, value: float) -> DimensionChange:
        d = self._doc(part=True)
        self._no_sketch_open(d)
        f, dname = self._split_dim(d, full_name)
        hint = "Appeler inspect_dimensions pour la liste des cotes."
        if f.sketch is not None:
            dim = next((x for x in f.sketch.dims if x.name == dname), None)
            if dim is None:
                raise invalid(f"Cote introuvable : {full_name[:80]}", hint=hint)
            old, dim.value, unit = dim.value, value, dim.unit
            self._apply_dim(f.sketch, dim)
        elif dname in f.dims:
            old, unit = f.dims[dname], f.dim_units[dname]
            f.dims[dname] = value
        else:
            raise invalid(f"Cote introuvable : {full_name[:80]}", hint=hint)
        self._rebuild(d)
        d.dirty = True
        return DimensionChange(full_name=full_name, old_value=rnd(old), new_value=value, unit=unit)

    def set_variable(self, name: str, value: str) -> VariableChange:
        d = self._doc()
        old = d.variables.get(name)
        d.variables[name] = value
        d.dirty = True
        return VariableChange(name=name, old_value=old, new_value=value)

    def rename_feature(self, name: str, new_name: str) -> str:
        d = self._doc()
        f = self._feat(d, name)
        self._name(d, "", new_name)
        for other in d.features:
            if other.params.get("sketch") == name:
                other.params["sketch"] = new_name
        f.name = new_name
        d.dirty = True
        return new_name

    def suppress_feature(self, name: str, suppressed: bool) -> bool:
        d = self._doc()
        self._no_sketch_open(d)
        self._feat(d, name).suppressed = suppressed
        self._rebuild(d)
        d.dirty = True
        return suppressed

    def feature_children(self, name: str) -> list[str]:
        d = self._doc()
        f = self._feat(d, name)
        out: list[str] = []
        for o in d.features:
            p = o.params
            deps = (
                p.get("sketch") == name
                or f.uid in p.get("sources", [])
                or any(k[0] == f.uid for k in p.get("edges", []) + p.get("faces", []))
                or bool(o.sketch and o.sketch.face_key and o.sketch.face_key[0] == f.uid)
            )
            if deps:
                out += [o.name, *[c for c in self.feature_children(o.name) if c != o.name]]
        return list(dict.fromkeys(out))

    def delete_feature(self, name: str, delete_children: bool) -> list[str]:
        d = self._doc()
        self._no_sketch_open(d)
        f = self._feat(d, name)
        victims = [name] + (self.feature_children(name) if delete_children else [])
        if not delete_children and f.params.get("sketch"):
            pass  # l'esquisse absorbée est conservée, comme dans SolidWorks
        d.features = [x for x in d.features if x.name not in victims]
        self._rebuild(d)
        d.dirty = True
        return victims

    # --- inspection ------------------------------------------------------------------------
    def tree(self, resolve: bool = False) -> list[FeatureNode]:
        d = self._doc()
        if d.type == "assembly":
            return [
                FeatureNode(name=c.name, type="Component", lightweight=c.lightweight and not resolve)
                for c in d.components
            ]
        nodes = (
            [FeatureNode(name=n, type="RefPlane") for n in PLANE_NAMES.values()] if d.type == "part" else []
        )
        if d.type == "part":
            nodes.append(FeatureNode(name="Origine", type="OriginProfileFeature"))
        owner = {f.params["sketch"]: f.name for f in d.features if f.params.get("sketch")}
        for f in d.features:
            nodes.append(
                FeatureNode(
                    name=f.name,
                    type=f.type,
                    suppressed=f.suppressed,
                    status="error" if f.error else "ok",
                    message=f.error,
                    parent=owner.get(f.name),
                )
            )
        return nodes

    def rebuild(self, force: bool = True) -> RebuildResult:
        d = self._doc()
        if d.type == "part" and not d.open_sketch:
            self._rebuild(d)
        errors = [
            RebuildIssue(feature=f.name, type=f.type, severity="error", code=1, message=f.error or "")
            for f in d.features
            if f.error
        ]
        under = [f.name for f in d.features if f.sketch and self._status(f.sketch) != "fully_defined"]
        dangling = [f.name for f in d.features if f.error and "pendante" in f.error]
        return RebuildResult(
            rebuild_ok=not errors, errors=errors, under_defined_sketches=under, dangling_references=dangling
        )

    def body_stats(self) -> BodyStats:
        d = self._doc()
        return BodyStats(
            body_count=d.bodies if d.volume > 1e-9 else 0,
            volume_mm3=rnd(d.volume),
            face_count=len(d.faces),
            edge_count=len(d.edges),
            bbox_mm=_bbox(d.bbox_pts),
        )

    def mass(self) -> MassResult:
        d = self._doc()
        if d.type == "drawing":
            raise SwError(
                ErrorCode.WRONG_DOC_TYPE, "Les propriétés de masse exigent une pièce ou un assemblage."
            )
        density = MATERIALS.get(d.material, 1.0) if d.material else None
        com = rnd3(g.scale(d.moment, 1 / d.volume)) if d.volume > 1e-9 else None
        return MassResult(
            volume_mm3=rnd(d.volume),
            surface_area_mm2=rnd(sum(f.area for f in d.faces.values())),
            mass_g=rnd(d.volume * density / 1000.0) if density else 0.0,
            center_of_mass_mm=com,
            bbox_mm=_bbox(d.bbox_pts),
            material=d.material,
            density_g_cm3=density,
            body_count=d.bodies if d.volume > 1e-9 else 0,
        )

    def dimensions(self) -> list[DimInfo]:
        d = self._doc()
        out: list[DimInfo] = []
        for f in d.features:
            if f.sketch:
                out += [
                    DimInfo(full_name=f"{x.name}@{f.name}", value=rnd(x.value), unit=x.unit, feature=f.name)
                    for x in f.sketch.dims
                ]
            out += [
                DimInfo(full_name=f"{k}@{f.name}", value=rnd(v), unit=f.dim_units[k], feature=f.name)
                for k, v in f.dims.items()
            ]
        return out

    def _fname(self, d: FDoc, uid: int) -> str | None:
        f = self._feat_by_uid(d, uid)
        return f.name if f else None

    def faces(self) -> list[FaceInfo]:
        d = self._doc(part=True)
        return [
            FaceInfo(
                handle=self.registry.register("face", d.id, f.key),
                surface=f.surface,
                area_mm2=rnd(f.area),
                centroid_mm=rnd3(f.centroid),
                normal=rnd3(f.normal) if f.normal else None,
                radius_mm=rnd(f.radius) if f.radius else None,
                feature=self._fname(d, f.feature),
            )
            for f in d.faces.values()
        ]

    def edges(self) -> list[EdgeInfo]:
        d = self._doc(part=True)
        out = []
        for e in d.edges.values():
            closed = e.curve == "circle"
            mid = e.pts[len(e.pts) // 2] if len(e.pts) > 2 else g.scale(g.add(e.pts[0], e.pts[-1]), 0.5)
            out.append(
                EdgeInfo(
                    handle=self.registry.register("edge", d.id, e.key),
                    curve=e.curve,
                    length_mm=rnd(e.length),
                    midpoint_mm=rnd3(e.center if closed and e.center else mid),
                    start_mm=None if closed else rnd3(e.pts[0]),
                    end_mm=None if closed else rnd3(e.pts[-1]),
                    center_mm=rnd3(e.center) if e.center else None,
                    radius_mm=rnd(e.radius) if e.radius else None,
                    faces=[self.registry.register("face", d.id, k) for k in e.faces if k in d.faces],
                )
            )
        return out

    def _geom(self, d: FDoc, x: str | Vec3) -> tuple[str, Vec3, Vec3 | None, str]:
        """(nature, point, direction ou normale, libellé) d'un opérande de mesure."""
        if not isinstance(x, str):
            return "point", x, None, "point"
        ref = self.registry.resolve(x, None, d.id)
        if ref.kind == "face":
            f = d.faces.get(ref.ref)  # type: ignore[call-overload]
            if f is None:
                raise stale(x)
            return ("plane" if f.normal else "point"), f.centroid, f.normal, f"face {f.surface}"
        if ref.kind == "edge":
            e = d.edges.get(ref.ref)  # type: ignore[call-overload]
            if e is None:
                raise stale(x)
            if e.curve == "line":
                return "line", e.pts[0], g.unit(g.sub(e.pts[-1], e.pts[0])), "arête droite"
            return "point", e.center or e.pts[0], None, "arête circulaire (centre)"
        raise invalid("inspect_measure accepte des handles de face ou d'arête, ou des points [x, y, z].")

    def measure(self, a: str | Vec3, b: str | Vec3) -> MeasureResult:
        d = self._doc(part=True)
        ka, pa, da, la = self._geom(d, a)
        kb, pb, db, lb = self._geom(d, b)
        angle = None
        if da is not None and db is not None:
            angle = g.angle_deg(da, db)
            if ka == kb == "line":
                angle = min(angle, 180 - angle)
        if (
            ka == "plane"
            and da is not None
            and (kb == "point" or (angle is not None and (angle < 1e-6 or abs(angle - 180) < 1e-6)))
        ):
            dist = abs(g.dot(g.sub(pb, pa), da))
        elif kb == "plane" and db is not None and ka == "point":
            dist = abs(g.dot(g.sub(pa, pb), db))
        elif (
            ka == "line"
            and da is not None
            and (kb == "point" or (kb == "line" and angle is not None and angle < 1e-6))
        ):
            dist = g.norm(g.cross(g.sub(pb, pa), da))
        elif kb == "line" and db is not None and ka == "point":
            dist = g.norm(g.cross(g.sub(pa, pb), db))
        elif ka == kb == "point":
            dist = g.dist(pa, pb)
        else:
            dist = 0.0 if ka == kb == "plane" else g.dist(pa, pb)
        return MeasureResult(
            distance_mm=rnd(dist), angle_deg=rnd(angle) if angle is not None else None, a=la, b=lb
        )

    def interference(self) -> list[InterferencePair]:
        d = self._doc()
        if d.type != "assembly":
            raise SwError(ErrorCode.WRONG_DOC_TYPE, "La détection d'interférences exige un assemblage.")
        out = []
        for i, a in enumerate(d.components):
            for b in d.components[i + 1 :]:
                dims = [min(a.bbox[1][k], b.bbox[1][k]) - max(a.bbox[0][k], b.bbox[0][k]) for k in range(3)]
                if all(x > 1e-9 for x in dims):
                    out.append(
                        InterferencePair(a=a.name, b=b.name, volume_mm3=rnd(dims[0] * dims[1] * dims[2]))
                    )
        return out

    def capture(self, views: list[str], width_px: int, zoom_to_fit: bool) -> list[bytes]:
        from PIL import Image, ImageDraw

        d = self._doc()
        height = max(1, width_px * 3 // 4)
        out = []
        for view in views:
            right, up = VIEW_DIRS[view]
            img = Image.new("RGB", (width_px, height), (236, 238, 240))
            lines = [[(g.dot(p, right), g.dot(p, up)) for p in e.pts] for e in d.edges.values()]
            flat = [q for ln in lines for q in ln]
            if flat:
                x0, x1 = min(q[0] for q in flat), max(q[0] for q in flat)
                y0, y1 = min(q[1] for q in flat), max(q[1] for q in flat)
                k = 0.85 * min(width_px / max(x1 - x0, 1e-6), height / max(y1 - y0, 1e-6))
                draw = ImageDraw.Draw(img)
                for ln in lines:
                    px = [
                        (width_px / 2 + (x - (x0 + x1) / 2) * k, height / 2 - (y - (y0 + y1) / 2) * k)
                        for x, y in ln
                    ]
                    draw.line(px, fill=(40, 60, 90), width=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            out.append(buf.getvalue())
        return out

    def export(self, fmt: str, path: str, options: dict[str, Any]) -> None:
        d = self._doc()
        if fmt == "png":
            Path(path).write_bytes(self.capture(["iso"], int(options.get("width_px", 1024)), True)[0])
            return
        body = f"SWMCP-FAKE-EXPORT format={fmt} doc={d.name} volume_mm3={d.volume:.3f}\n"
        Path(path).write_text(body, encoding="utf-8")

    def raw_call(self, target: str, method: str, args: list[Any]) -> Any:
        d = self.active
        self._tick()
        table: dict[str, Any] = {"app.RevisionNumber": self.revision, "app.GetProcessID": self.pid}
        if d is not None:
            table.update(
                {
                    "doc.GetTitle": d.name,
                    "doc.GetPathName": d.path or "",
                    "doc.GetFeatureCount": len(d.features),
                }
            )
        key = f"{target}.{method}"
        if key not in table:
            raise SwError(ErrorCode.UNSUPPORTED_VERSION, f"Membre non simulé : {key}")
        return table[key]
