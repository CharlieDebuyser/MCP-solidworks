"""Tests d'intégration sur un vrai SolidWorks (spec §12, §14).

Ignorés par défaut (marqueur `solidworks`, voir pyproject). À lancer sur un poste Windows sous
licence, SolidWorks ouvert :

    pytest -m solidworks tests/integration/test_solidworks.py -p no:cacheprovider

Ces tests n'ont jamais tourné (v1 sans test réel) : ils sont la première vérification de
`ComSwBackend`, dont chaque appel COM est listé dans `docs/unverified.md`. Chaque test crée son
propre document et le ferme sans enregistrer, même en cas d'échec.

Variables lues à la collecte (avant la purge des `SWMCP_*` par `conftest.py`) :
- `SWMCP_TEST_TEMPLATE_PART` : modèle de pièce à utiliser si aucun modèle par défaut n'est configuré ;
- `SWMCP_TEST_LAUNCH=1` : lancer SolidWorks s'il n'est pas ouvert.
"""

from __future__ import annotations

import contextlib
import io
import math
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from swmcp.backend import capabilities as caps
from swmcp.backend import geometry as g
from swmcp.backend.constants import C
from swmcp.backend.models import DocRef, FaceInfo, Vec3
from swmcp.envelope import ErrorCode, SwError

pytestmark = pytest.mark.solidworks

TEMPLATE_PART = os.environ.get("SWMCP_TEST_TEMPLATE_PART") or None
LAUNCH = os.environ.get("SWMCP_TEST_LAUNCH") == "1"

PLANE_NORMALS: dict[str, Vec3] = {"front": (0.0, 0.0, 1.0), "top": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0)}
ENGLISH_PLANE_NAMES = {"Front Plane", "Top Plane", "Right Plane"}
BOX = (40.0, 30.0, 10.0)


# --- fixtures -----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sw() -> Iterator[Any]:
    """Backend connecté, créé et utilisé dans le thread du test (appartement COM mono-thread)."""
    pythoncom = pytest.importorskip("pythoncom", reason="pywin32 absent : tests réservés à Windows")
    from swmcp.backend.com_backend import ComSwBackend

    pythoncom.CoInitialize()
    backend = ComSwBackend()
    try:
        backend.connect(launch_if_needed=LAUNCH)
        yield backend
    finally:
        backend.shutdown()
        pythoncom.CoUninitialize()


def _close_quietly(sw: Any, doc_id: str) -> None:
    """Ferme sans enregistrer le document créé par le test, et lui seul (jamais celui de l'utilisateur)."""
    with contextlib.suppress(SwError):
        sw.cleanup()
    with contextlib.suppress(SwError):
        ours = [d for d in sw.list_docs() if d.id == doc_id]
        for doc in ours:
            if doc.active and sw.state().sketch_open:
                sw.sketch_end()
            sw.close_doc(doc.name, save=False)


@pytest.fixture
def part(sw: Any) -> Iterator[DocRef]:
    """Une pièce neuve par test, fermée sans enregistrer quoi qu'il arrive."""
    ref: DocRef = sw.new_doc("part", TEMPLATE_PART)
    try:
        yield ref
    finally:
        _close_quietly(sw, ref.id)


def _box(sw: Any, plane: str = "front", size: Vec3 = BOX, name: str = "Bloc") -> Any:
    """Pavé size[0] × size[1] dans l'esquisse, extrudé de size[2]. Renvoie le repère de l'esquisse."""
    begun = sw.sketch_begin(plane, "Esq")
    sw.sketch_rect(0.0, 0.0, size[0], size[1])
    ended = sw.sketch_end()
    assert ended.closed, ended.issues
    sw.extrude("Esq", size[2], "blind", "normal", True, 0.0, name, False)
    sw.cleanup()
    return begun.frame


def _parallel(a: Vec3, b: Vec3) -> bool:
    return abs(abs(g.dot(g.unit(a), g.unit(b))) - 1.0) < 1e-6


def _far_face(sw: Any, normal: Vec3) -> FaceInfo:
    """Face plane perpendiculaire à `normal` la plus éloignée de l'origine le long de cette direction."""
    faces: list[FaceInfo] = sw.faces()
    candidates = [f for f in faces if f.surface == "plane" and f.normal and _parallel(f.normal, normal)]
    assert len(candidates) >= 2, "un pavé a deux faces perpendiculaires à chaque axe"
    return max(candidates, key=lambda f: abs(g.dot(f.centroid_mm, normal)))


# --- connexion ----------------------------------------------------------------------------------
def test_connect_reports_version_and_instances(sw: Any) -> None:
    result = sw.connect()
    major, _, _ = caps.parse_revision(result.revision)
    assert result.version == str(caps.sw_year(major))
    assert major >= caps.MIN_SUPPORTED_MAJOR, "la v1 vise SOLIDWORKS 2023 et suivants"
    assert result.pid > 0 and any(i.pid == result.pid for i in result.instances)
    assert result.language
    assert sw.is_connected() and sw.ping() is True
    assert all(caps.summary(major).values())


def test_connect_unknown_pid_is_refused(sw: Any) -> None:
    with pytest.raises(SwError) as err:
        sw.connect(pid=1)  # le pid 1 n'est jamais SolidWorks
    assert err.value.code is ErrorCode.INVALID_INPUT
    assert sw.is_connected(), "un pid refusé ne doit pas rompre la connexion existante"


def test_state_never_raises(sw: Any, part: DocRef) -> None:
    state = sw.state()
    assert state.doc == part.name and state.doc_type == "part"
    assert state.sketch_open is None and state.rebuild_ok is True
    assert state.feature_count is not None and state.feature_count > 0


# --- documents ----------------------------------------------------------------------------------
def test_new_save_close_open_read_only(sw: Any, part: DocRef, tmp_path: Path) -> None:
    assert part.type == "part" and part.path is None and part.active
    with pytest.raises(SwError) as err:
        sw.save_doc(None)
    assert err.value.code is ErrorCode.INVALID_INPUT, "un document jamais enregistré exige un chemin"
    _box(sw)
    target = tmp_path / "bloc.sldprt"
    assert sw.save_doc(str(target)) == str(target)
    assert target.is_file() and target.stat().st_size > 0
    info = sw.doc_info()
    assert info.path is not None and Path(info.path) == target and not info.dirty
    assert info.id == part.id, "l'identifiant du document survit au changement de titre"
    sw.close_doc(None, save=False)

    opened = sw.open_doc(str(target), read_only=True)
    try:
        assert opened.type == "part" and opened.read_only and opened.open_errors == []
        assert any(d.path and Path(d.path) == target and d.active for d in sw.list_docs())
        assert sw.body_stats().volume_mm3 == pytest.approx(BOX[0] * BOX[1] * BOX[2], rel=0.01)
        with pytest.raises(SwError) as denied:
            sw.save_doc(None)
        assert denied.value.code is ErrorCode.READ_ONLY_MODE
    finally:
        _close_quietly(sw, opened.id)


def test_open_missing_file_is_a_coded_error(sw: Any, tmp_path: Path) -> None:
    before = len(sw.list_docs())
    with pytest.raises(SwError) as err:
        sw.open_doc(str(tmp_path / "absent.sldprt"), read_only=True)
    assert err.value.code is ErrorCode.INVALID_INPUT
    assert err.value.details is not None and err.value.details["open_errors"]
    assert len(sw.list_docs()) == before


def test_open_corrupt_file_is_a_coded_error(sw: Any, tmp_path: Path) -> None:
    """Piège §10 : les erreurs de OpenDoc6 reviennent par référence ; un fichier corrompu ne doit ni
    lever une erreur COM brute ni bloquer sur un dialogue."""
    corrupt = tmp_path / "corrompu.sldprt"
    corrupt.write_bytes(b"CORRUPT ceci n'est pas une piece SolidWorks " * 64)
    before = len(sw.list_docs())
    with pytest.raises(SwError) as err:
        sw.open_doc(str(corrupt), read_only=True)
    assert err.value.code is ErrorCode.INVALID_INPUT, err.value.message
    assert err.value.details is not None and err.value.details["open_errors"]
    assert len(sw.list_docs()) == before
    assert sw.ping()


def test_unknown_extension_and_missing_template(sw: Any, tmp_path: Path) -> None:
    with pytest.raises(SwError) as err:
        sw.open_doc(str(tmp_path / "plan.txt"), read_only=True)
    assert err.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(SwError) as template:
        sw.new_doc("part", str(tmp_path / "absent.prtdot"))
    assert template.value.code is ErrorCode.INVALID_INPUT and "template_path" in template.value.hint


def test_custom_properties_roundtrip(sw: Any, part: DocRef) -> None:
    change = sw.set_prop("Client", "ACME")
    assert (change.before, change.after) == (None, "ACME")
    assert sw.get_props()["Client"] == "ACME"
    assert sw.set_prop("Client", "Autre").before == "ACME"
    removed = sw.set_prop("Client", None)
    assert removed.before == "Autre" and removed.after is None
    assert "Client" not in sw.get_props()


# --- esquisse, extrusion, mesures ---------------------------------------------------------------
def test_sketch_extrude_volume_and_bbox(sw: Any, part: DocRef) -> None:
    begun = sw.sketch_begin("front", None)
    assert begun.name == "Sketch_1" and begun.plane == "front"
    assert sw.state().sketch_open == "Sketch_1"
    refs = sw.sketch_rect(0.0, 0.0, BOX[0], BOX[1])
    assert [r.role for r in refs] == [
        "bottom", "right", "top", "left", "bottom_left", "bottom_right", "top_right", "top_left",
    ]  # fmt: skip
    assert len({r.handle for r in refs}) == 8, "les coins fusionnés gardent quatre points distincts"
    inspected = sw.sketch_inspect(None)
    assert inspected.closed and len([s for s in inspected.segments if s.type == "line"]) == 4
    assert {r.type for r in inspected.relations} >= {"horizontal", "vertical"}
    ended = sw.sketch_end()
    assert ended.closed and ended.segment_count == 4 and ended.status == "under_defined"
    assert sw.state().sketch_open is None

    created = sw.extrude("Sketch_1", BOX[2], "blind", "normal", True, 0.0, None, False)
    assert created.feature == "Extrude_1" and created.type == "Extrude" and created.sketch == "Sketch_1"
    stats = sw.body_stats()
    assert stats.body_count == 1 and stats.face_count == 6 and stats.edge_count == 12
    assert stats.volume_mm3 == pytest.approx(BOX[0] * BOX[1] * BOX[2], rel=0.01)
    assert stats.bbox_mm is not None
    assert sorted(stats.bbox_mm.size) == pytest.approx(sorted(BOX), abs=0.1)
    names = {n.name: n for n in sw.tree()}
    assert {"Sketch_1", "Extrude_1"} <= set(names) and names["Extrude_1"].status == "ok"
    assert sw.rebuild().rebuild_ok
    assert created.dimensions, "l'extrusion borgne porte une cote de profondeur"
    depth = next(d for d in sw.dimensions() if d.full_name == created.dimensions[0])
    assert depth.value == pytest.approx(BOX[2]) and depth.unit == "mm"


def test_dimension_set_changes_the_volume(sw: Any, part: DocRef) -> None:
    sw.sketch_begin("front", "Esq")
    sw.sketch_rect(0.0, 0.0, BOX[0], BOX[1])
    sw.sketch_end()
    created = sw.extrude("Esq", BOX[2], "blind", "normal", True, 0.0, "Bloc", False)
    change = sw.set_dimension(created.dimensions[0], 2 * BOX[2])
    assert change.old_value == pytest.approx(BOX[2]) and change.new_value == 2 * BOX[2]
    assert sw.body_stats().volume_mm3 == pytest.approx(2 * BOX[0] * BOX[1] * BOX[2], rel=0.01)
    with pytest.raises(SwError) as err:
        sw.set_dimension("D99@Bloc", 5.0)
    assert err.value.code is ErrorCode.INVALID_INPUT


def test_sketch_dimension_drives_geometry(sw: Any, part: DocRef) -> None:
    sw.sketch_begin("front", "Esq")
    refs = {r.role: r.handle for r in sw.sketch_rect(0.0, 0.0, BOX[0], BOX[1])}
    assert sw.sketch_relation("coincident", [refs["bottom_left"], "origin"]).relation == "coincident"
    width = sw.sketch_dimension("length", [refs["bottom"]], 50.0, "Largeur")
    assert width.full_name == "Largeur@Esq" and width.unit == "mm"
    height = sw.sketch_dimension("length", [refs["right"]], 20.0, "Hauteur")
    assert height.status == "fully_defined"
    assert sw.sketch_end().status == "fully_defined"
    sw.extrude("Esq", BOX[2], "blind", "normal", True, 0.0, "Bloc", False)
    assert sw.body_stats().volume_mm3 == pytest.approx(50.0 * 20.0 * BOX[2], rel=0.01)


def test_mass_without_material_is_zero(sw: Any, part: DocRef) -> None:
    """Piège §10 : sans matériau, volume correct, masse 0 et matériau nul."""
    _box(sw)
    mass = sw.mass()
    assert mass.material is None and mass.mass_g == 0.0 and mass.density_g_cm3 is None
    assert mass.volume_mm3 == pytest.approx(BOX[0] * BOX[1] * BOX[2], rel=0.01)
    assert mass.surface_area_mm2 == pytest.approx(2 * (40 * 30 + 40 * 10 + 30 * 10), rel=0.01)
    assert mass.center_of_mass_mm is not None and mass.body_count == 1
    assert sw.doc_info().material is None


# --- pièges §10 : repère d'esquisse, plans de référence ------------------------------------------
@pytest.mark.parametrize("plane", ["front", "top", "right"])
def test_reference_planes_found_by_type_and_order(sw: Any, part: DocRef, plane: str) -> None:
    """Les plans sont repérés par type et ordre dans l'arbre : le test doit passer en français."""
    begun = sw.sketch_begin(plane, None)
    frame = begun.frame
    assert _parallel(frame.normal, PLANE_NORMALS[plane]), f"{plane} : normale {frame.normal}"
    assert g.norm(frame.origin) == pytest.approx(0.0, abs=1e-6)
    assert g.dot(frame.x_axis, frame.y_axis) == pytest.approx(0.0, abs=1e-6)
    assert g.cross(frame.x_axis, frame.y_axis) == pytest.approx(frame.normal, abs=1e-6), "repère direct"
    assert sw.sketch_end().status == "failed", "une esquisse vide est supprimée"
    assert begun.name not in {n.name for n in sw.tree()}


def test_reference_plane_names_are_localized_data(sw: Any, part: DocRef) -> None:
    language = sw.connect().language
    planes = [n for n in sw.tree() if n.type == "RefPlane"][:3]
    assert len(planes) == 3
    if language == "english":
        pytest.skip("installation anglophone : ce test ne prouve rien sur les noms localisés")
    assert not ({p.name for p in planes} & ENGLISH_PLANE_NAMES), (
        "noms anglais sur une installation non anglophone ?"
    )
    for key in PLANE_NORMALS:  # et pourtant les trois plans se désignent par leur clé
        assert _parallel(sw.sketch_begin(key, None).frame.normal, PLANE_NORMALS[key])
        sw.sketch_end()


@pytest.mark.parametrize("plane", ["front", "top", "right"])
def test_hole_lands_where_asked_in_model_coordinates(sw: Any, part: DocRef, plane: str) -> None:
    """Piège « repère de l'esquisse » : percer à (x, y, z) modèle, puis mesurer la position obtenue."""
    _box(sw, plane)
    normal = PLANE_NORMALS[plane]
    face = _far_face(sw, normal)
    tangents = [axis for axis in PLANE_NORMALS.values() if not _parallel(axis, normal)]
    target = g.add(face.centroid_mm, g.add(g.scale(tangents[0], 5.0), g.scale(tangents[1], -3.0)))
    before = sw.body_stats().volume_mm3

    created = sw.hole(face.handle, [target], 6.0, None, "Trou")
    assert created.feature == "Trou" and created.type == "CutExtrude"
    removed = before - sw.body_stats().volume_mm3
    assert removed == pytest.approx(math.pi * 3.0**2 * BOX[2], rel=0.01), "perçage débouchant de Ø6"

    rims = [e for e in sw.edges() if e.curve == "circle" and e.radius_mm == pytest.approx(3.0, abs=1e-3)]
    assert len(rims) == 2, "un perçage débouchant a deux arêtes circulaires"
    for rim in rims:
        assert rim.center_mm is not None
        offset = g.sub(rim.center_mm, target)
        for tangent in tangents:
            assert g.dot(offset, tangent) == pytest.approx(0.0, abs=0.1), (
                f"centre {rim.center_mm}, visé {target}"
            )
    walls = [f for f in sw.faces() if f.surface == "cylinder" and f.radius_mm == pytest.approx(3.0, abs=1e-3)]
    assert walls and all(f.feature == "Trou" for f in walls)
    entry = min(rims, key=lambda e: g.dist(e.center_mm or (0.0, 0.0, 0.0), target))
    assert sw.measure(target, entry.center_mm or target).distance_mm == pytest.approx(0.0, abs=0.1)


def test_sketch_on_face_reports_its_own_frame(sw: Any, part: DocRef) -> None:
    _box(sw)
    face = _far_face(sw, PLANE_NORMALS["front"])
    begun = sw.sketch_begin(face.handle, "SurFace")
    assert begun.plane == "face" and _parallel(begun.frame.normal, PLANE_NORMALS["front"])
    # Un point de l'esquisse, reporté dans le modèle par le repère renvoyé, doit être sur la face.
    on_face = g.to_model(begun.frame, 4.0, 7.0)
    assert g.dot(g.sub(on_face, face.centroid_mm), PLANE_NORMALS["front"]) == pytest.approx(0.0, abs=1e-3)
    sw.sketch_circle(4.0, 7.0, 2.0)
    assert sw.sketch_end().closed


# --- erreurs codées et retour à un état propre --------------------------------------------------
def test_open_profile_gives_sketch_not_closed(sw: Any, part: DocRef) -> None:
    sw.sketch_begin("front", "Ouvert")
    sw.sketch_polyline([(0.0, 0.0), (20.0, 0.0), (20.0, 10.0)], closed=False)
    ended = sw.sketch_end()
    assert not ended.closed and ended.issues
    count = sw.state().feature_count
    with pytest.raises(SwError) as err:
        sw.extrude("Ouvert", 5.0, "blind", "normal", True, 0.0, None, False)
    assert err.value.code is ErrorCode.SKETCH_NOT_CLOSED
    assert err.value.details is not None and err.value.details["sketch"] == "Ouvert"
    assert sw.state().feature_count == count, "aucune fonction ne doit rester après l'échec"


def test_silent_feature_failure_carries_a_diagnostic(sw: Any, part: DocRef) -> None:
    """Piège « échecs silencieux » : un enlèvement qui ne rencontre aucune matière."""
    _box(sw)
    sw.sketch_begin("front", "Ailleurs")
    sw.sketch_circle(500.0, 500.0, 2.0)
    sw.sketch_end()
    with pytest.raises(SwError) as err:
        sw.extrude("Ailleurs", None, "through_all", "both", True, 0.0, "Rate", True)
    assert err.value.code is ErrorCode.FEATURE_FAILED
    details = err.value.details or {}
    assert details["feature"] == "Rate" and details["type"] == "CutExtrude" and details["kept"] is False
    assert details["sw_error"] and details["sketch"] == {
        "name": "Ailleurs",
        "closed": True,
        "status": "under_defined",
    }
    assert "Rate" not in {n.name for n in sw.tree()}
    assert sw.rebuild().rebuild_ok


def test_feature_tool_refuses_while_a_sketch_is_open(sw: Any, part: DocRef) -> None:
    _box(sw)
    sw.sketch_begin("front", "EnCours")
    with pytest.raises(SwError) as err:
        sw.extrude("Esq", 5.0, "blind", "normal", True, 0.0, None, False)
    assert err.value.code is ErrorCode.SKETCH_OPEN
    assert sw.state().sketch_open == "EnCours", "l'esquisse n'est jamais fermée en silence"
    sw.sketch_end()


def test_stale_handles(sw: Any, part: DocRef) -> None:
    _box(sw)
    face = _far_face(sw, PLANE_NORMALS["front"])
    edge = sw.edges()[0]
    assert sw.measure(face.handle, face.centroid_mm).distance_mm == pytest.approx(0.0, abs=1e-3)
    with pytest.raises(SwError) as unknown:
        sw.measure("ent:face:999999", (0.0, 0.0, 0.0))
    assert unknown.value.code is ErrorCode.ENTITY_STALE
    with pytest.raises(SwError) as wrong_kind:
        sw.fillet([face.handle], 1.0, None)
    assert wrong_kind.value.code is ErrorCode.INVALID_INPUT

    assert sw.delete_feature("Bloc", delete_children=False) == ["Bloc"]
    for handle in (face.handle, edge.handle):
        with pytest.raises(SwError) as err:
            sw.measure(handle, (0.0, 0.0, 0.0))
        assert err.value.code is ErrorCode.ENTITY_STALE, "l'identifiant persistant ne se résout plus"


def test_handles_die_with_their_document(sw: Any) -> None:
    ref = sw.new_doc("part", TEMPLATE_PART)
    try:
        _box(sw)
        handle = sw.faces()[0].handle
    finally:
        _close_quietly(sw, ref.id)
    other = sw.new_doc("part", TEMPLATE_PART)
    try:
        with pytest.raises(SwError) as err:
            sw.measure(handle, (0.0, 0.0, 0.0))
        assert err.value.code is ErrorCode.ENTITY_STALE
    finally:
        _close_quietly(sw, other.id)


def test_cleanup_restores_selection_and_sketch_inference(sw: Any, part: DocRef) -> None:
    inference = sw.raw_call("app", "GetUserPreferenceToggle", [C.swSketchInference])
    sw.sketch_begin("front", "Esq")
    sw.sketch_line(0.0, 0.0, 10.0, 0.0, False)
    sw.cleanup()
    assert sw.raw_call("app", "GetUserPreferenceToggle", [C.swSketchInference]) == inference
    assert sw.state().sketch_open == "Esq", "cleanup ne ferme pas une esquisse légitimement ouverte"
    sw.sketch_end()
    _ = sw.faces()
    sw.cleanup()
    sw.cleanup()  # idempotent, ne lève jamais


def test_fillet_then_chamfer_on_found_edges(sw: Any, part: DocRef) -> None:
    _box(sw)
    volume = sw.body_stats().volume_mm3
    vertical = [e for e in sw.edges() if e.curve == "line" and e.length_mm == pytest.approx(BOX[2], abs=1e-3)]
    assert len(vertical) == 4
    fillet = sw.fillet([vertical[0].handle], 2.0, None)
    assert fillet.feature == "Fillet_1" and fillet.type == "Fillet"
    expected = (1 - math.pi / 4) * 2.0**2 * BOX[2]
    assert volume - sw.body_stats().volume_mm3 == pytest.approx(expected, rel=0.02)
    with pytest.raises(SwError) as too_big:
        sw.fillet([e.handle for e in sw.edges() if e.curve == "line"][:1], 500.0, None)
    assert too_big.value.code is ErrorCode.FEATURE_FAILED and "Fillet_2" not in {n.name for n in sw.tree()}


# --- export et capture --------------------------------------------------------------------------
def test_export_step(sw: Any, part: DocRef, tmp_path: Path) -> None:
    _box(sw)
    target = tmp_path / "bloc.step"
    sw.export("step", str(target), {})
    assert target.is_file() and target.stat().st_size > 0
    assert target.read_bytes()[:12] == b"ISO-10303-21"
    assert sw.doc_info().path is None, "un export ne change pas le document actif"
    with pytest.raises(SwError) as err:
        sw.export("step", str(tmp_path / "bloc.txt"), {})
    assert err.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(SwError) as wrong:
        sw.export("pdf", str(tmp_path / "bloc.pdf"), {})
    assert wrong.value.code is ErrorCode.WRONG_DOC_TYPE


def test_save_copy_keeps_the_active_document(sw: Any, part: DocRef, tmp_path: Path) -> None:
    _box(sw)
    copy = tmp_path / "copie.sldprt"
    sw.save_copy(str(copy))
    assert copy.is_file() and copy.stat().st_size > 0
    state = sw.state()
    assert state.doc == part.name and sw.doc_info().path is None


def test_capture_png(sw: Any, part: DocRef, tmp_path: Path) -> None:
    from PIL import Image

    _box(sw)
    images = sw.capture(["iso", "front"], 640, True)
    assert len(images) == 2
    for data in images:
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (640, 480)
            extrema: Any = image.convert("L").getextrema()
            assert extrema[1] > extrema[0], "image uniforme : la pièce n'apparaît pas"
    assert images[0] != images[1], "deux vues différentes donnent deux images différentes"
    target = tmp_path / "vue.png"
    sw.export("png", str(target), {"width_px": 320})
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(SwError) as err:
        sw.capture(["dessous-gauche"], 640, True)
    assert err.value.code is ErrorCode.INVALID_INPUT
