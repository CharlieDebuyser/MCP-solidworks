"""Unités, enveloppe, chemins, confirmations, journal, points de restauration, registre, géométrie."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from swmcp.backend import geometry as g
from swmcp.backend import units
from swmcp.backend.entities import EntityRegistry
from swmcp.backend.models import SketchSegment
from swmcp.config import load_settings
from swmcp.envelope import RETRYABLE, DocState, Envelope, ErrorCode, SwError
from swmcp.safety.audit import AuditLog, truncate
from swmcp.safety.checkpoints import CheckpointManager
from swmcp.safety.confirm import ConfirmStore
from swmcp.safety.paths import EXT_MODELS, PathGuard, lexical_check


# --- unités ---------------------------------------------------------------------------------
def test_units_roundtrip() -> None:
    assert units.mm_to_m(1500) == 1.5
    assert units.m_to_mm(0.003) == pytest.approx(3.0)
    assert units.deg_to_rad(180) == pytest.approx(math.pi)
    assert units.rad_to_deg(math.pi / 2) == pytest.approx(90)
    assert units.m3_to_mm3(1e-6) == pytest.approx(1000)
    assert units.m2_to_mm2(1e-4) == pytest.approx(100)
    assert units.kg_to_g(0.5) == 500
    assert units.kg_m3_to_g_cm3(7850) == pytest.approx(7.85)
    assert units.vec3_m_to_mm((0.001, 0.002, -0.003)) == pytest.approx((1, 2, -3))
    assert units.rnd(-0.0000000001) == 0.0 and str(units.rnd(-1e-12)) == "0.0"


# --- enveloppe ------------------------------------------------------------------------------
def test_error_defaults() -> None:
    err = SwError(ErrorCode.ENTITY_STALE, "x")
    assert not err.retryable and "find_faces" in err.hint
    assert SwError(ErrorCode.SW_BUSY, "x").retryable
    assert {c.value for c in RETRYABLE} == {
        "NOT_CONNECTED",
        "SW_DISCONNECTED",
        "SW_BUSY",
        "CONFIRMATION_REQUIRED",
    }
    assert len(ErrorCode) == 18


def test_envelope_shape() -> None:
    env = Envelope[dict](ok=True, data={"a": 1}, state=DocState(doc="p.sldprt"), elapsed_ms=3)
    dumped = env.model_dump(mode="json")
    assert list(dumped) == ["ok", "data", "warnings", "error", "state", "elapsed_ms"]
    assert list(dumped["state"]) == ["doc", "doc_type", "dirty", "rebuild_ok", "feature_count", "sketch_open"]


# --- configuration --------------------------------------------------------------------------
def test_settings_env_and_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SWMCP_ALLOWED_DIRS", f"{tmp_path / 'a'};{tmp_path / 'b'}")
    monkeypatch.setenv("SWMCP_PROFILE", "inspect")
    s = load_settings()
    assert s.allowed_dirs == [tmp_path / "a", tmp_path / "b"] and s.read_only
    assert load_settings(profile="full").profile == "full"  # la ligne de commande prime
    monkeypatch.delenv("SWMCP_ALLOWED_DIRS")
    s2 = load_settings(workspace=tmp_path / "w")
    assert s2.allowed_dirs == [tmp_path / "w"] and s2.timeout_s == 60 and not s2.raw_api
    assert s2.allows("inspect") and s2.allows("design") is False


def test_settings_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('profile = "full"\ntimeout_s = 12\n', encoding="utf-8")
    monkeypatch.setenv("SWMCP_CONFIG", str(cfg))
    assert load_settings().timeout_s == 12
    monkeypatch.setenv("SWMCP_TIMEOUT_S", "30")
    assert load_settings().timeout_s == 30 and load_settings().profile == "full"


def test_settings_rejects_http() -> None:
    with pytest.raises(ValueError):
        load_settings(transport="http")


# --- chemins --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "..\\secret.sldprt",
        "a/../../b.sldprt",
        "\\\\serveur\\partage\\p.sldprt",
        "//host/p.sldprt",
        "\\\\.\\C:\\p.sldprt",
        "C:\\tmp\\NUL",
        "com1.sldprt",
        "dir/LPT3.txt/p.sldprt",
        "p.sldprt:flux",
        "a\x00b.sldprt",
        "p?.sldprt",
        "dossier./p.sldprt",
        "D:rel.sldprt",
    ],
)
def test_lexical_rejects(bad: str) -> None:
    with pytest.raises(SwError) as exc:
        lexical_check(bad)
    assert exc.value.code == ErrorCode.PATH_DENIED


def test_lexical_accepts() -> None:
    for good in ["C:\\Projets\\CAO\\p.sldprt", "/tmp/a b/p.sldprt", "sous/dossier/p.STEP", "./p.sldprt"]:
        lexical_check(good)


def test_path_guard(tmp_path: Path) -> None:
    allowed, outside = tmp_path / "ok", tmp_path / "ko"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "p.sldprt").write_text("x")
    (outside / "p.sldprt").write_text("x")
    guard = PathGuard([allowed], allowed)
    assert guard.check(str(allowed / "p.sldprt"), extensions=EXT_MODELS, must_exist=True) == Path(
        os.path.realpath(allowed / "p.sldprt")
    )
    assert guard.check("p.sldprt", must_exist=True).parent == Path(
        os.path.realpath(allowed)
    )  # relatif au dossier de travail
    for raw in (str(outside / "p.sldprt"), str(tmp_path / "okk" / "p.sldprt")):
        with pytest.raises(SwError) as exc:
            guard.check(raw)
        assert exc.value.code == ErrorCode.PATH_DENIED
    with pytest.raises(SwError) as exc:
        guard.check(str(allowed / "p.exe"), extensions=EXT_MODELS)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    with pytest.raises(SwError) as exc:
        guard.check(str(allowed / "absent.sldprt"), must_exist=True)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert guard.check(str(allowed / "new" / "x.step"), write=True).parent.is_dir()


def test_path_guard_symlink_escape(tmp_path: Path) -> None:
    allowed, outside = tmp_path / "ok", tmp_path / "ko"
    allowed.mkdir()
    outside.mkdir()
    (outside / "p.sldprt").write_text("x")
    (allowed / "lien").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SwError) as exc:
        PathGuard([allowed], allowed).check(str(allowed / "lien" / "p.sldprt"))
    assert exc.value.code == ErrorCode.PATH_DENIED


# --- confirmations --------------------------------------------------------------------------
def test_confirm_protocol() -> None:
    now = [0.0]
    store = ConfirmStore(ttl_s=120, clock=lambda: now[0])
    params = {"name": "Base", "delete_children": False}
    with pytest.raises(SwError) as exc:
        store.require("feature_delete", params, None, "effet")
    assert exc.value.code == ErrorCode.CONFIRMATION_REQUIRED and exc.value.retryable
    token = exc.value.details["confirm_token"]  # type: ignore[index]
    assert not store.consume("feature_delete", {**params, "name": "Autre"}, token)  # lié aux paramètres
    assert not store.consume("doc_close", params, token)  # lié à l'outil
    assert not store.consume("feature_delete", params, "faux")
    store.require("feature_delete", {**params, "confirm_token": token}, token, "effet")  # accepté
    assert not store.consume("feature_delete", params, token)  # usage unique


def test_confirm_expiry() -> None:
    now = [0.0]
    store = ConfirmStore(ttl_s=120, clock=lambda: now[0])
    token = store.issue("t", {})
    now[0] = 121
    assert not store.consume("t", {}, token)
    with pytest.raises(SwError) as exc:
        store.require("t", {}, token, "effet")
    assert "expiré" in exc.value.message


# --- journal --------------------------------------------------------------------------------
def test_audit_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(
        tool="t",
        params={"path": "x" * 500, "blob": b"abc", "confirm_token": "secret"},
        code="OK",
        elapsed_ms=4,
        doc="d",
    )
    log.close()
    line = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert line["tool"] == "t" and line["code"] == "OK" and line["session"] == log.session_id
    assert len(line["params"]["path"]) <= 201 and line["params"]["blob"] == "<3 octets>"
    assert line["params"]["confirm_token"] == "***"


def test_truncate_nested() -> None:
    assert truncate(list(range(50)))[-1] == "… (+30)"
    assert truncate({"a": {"b": {"c": {"d": {"e": 1}}}}})["a"]["b"]["c"]["d"] == "…"


# --- points de restauration -------------------------------------------------------------------
def test_checkpoints(tmp_path: Path) -> None:
    mgr = CheckpointManager(tmp_path, max_per_doc=3, max_total_mb=1)
    made = [
        mgr.create("support.sldprt", f"etape{i}", lambda p: Path(p).write_bytes(b"x" * 10)) for i in range(5)
    ]
    listed = mgr.list("support.sldprt")
    assert len(listed) == 3 and listed[0].id == made[-1].id
    assert made[-1].path.startswith(str(tmp_path / ".swmcp" / "checkpoints" / "support"))
    assert mgr.resolve(made[-1].id).is_file()
    for bad in ("../x", "support", "support/../../x", "support/absent.sldprt"):
        with pytest.raises(SwError):
            mgr.resolve(bad)
    with pytest.raises(SwError):
        mgr.create("a.sldprt", "x", lambda p: None)  # copie non écrite


def test_checkpoints_total_cap(tmp_path: Path) -> None:
    mgr = CheckpointManager(tmp_path, max_per_doc=20, max_total_mb=0.001)  # ~1 ko
    for i in range(4):
        mgr.create(f"d{i}.sldprt", "x", lambda p: Path(p).write_bytes(b"x" * 600))
    assert len(mgr.list()) == 1


# --- registre d'entités -----------------------------------------------------------------------
def test_registry() -> None:
    reg = EntityRegistry()
    h1 = reg.register("face", "doc1", b"ref-1")
    assert h1 == "ent:face:1" and reg.register("face", "doc1", b"ref-1") == h1
    h2 = reg.register("edge", "doc2", b"ref-1")
    assert reg.resolve(h1, "face", "doc1").ref == b"ref-1"
    for args, code in (
        (("ent:face:99",), ErrorCode.ENTITY_STALE),
        (("face-1",), ErrorCode.INVALID_INPUT),
        ((h1, "edge"), ErrorCode.INVALID_INPUT),
        ((h1, "face", "doc2"), ErrorCode.ENTITY_STALE),
    ):
        with pytest.raises(SwError) as exc:
            reg.resolve(*args)
        assert exc.value.code == code
    assert reg.invalidate_doc("doc1") == 1
    with pytest.raises(SwError):
        reg.resolve(h1)
    reg.clear()
    with pytest.raises(SwError):
        reg.resolve(h2)
    assert reg.register("face", "doc1", b"ref-1") == "ent:face:3"  # numéros jamais réutilisés


# --- géométrie ------------------------------------------------------------------------------
def _line(i: int, a: tuple[float, float], b: tuple[float, float]) -> SketchSegment:
    return SketchSegment(handle=f"ent:sk:{i}", type="line", start=a, end=b)


def _poly(pts: list[tuple[float, float]]) -> list[SketchSegment]:
    return [_line(i, pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def test_plane_frames_match_solidworks() -> None:
    front, top, right = (g.frame_for_plane(g.PLANE_NORMALS[p]) for p in ("front", "top", "right"))
    assert (front.x_axis, front.y_axis) == ((1, 0, 0), (0, 1, 0))
    assert (top.x_axis, top.y_axis) == ((1, 0, 0), (0, 0, -1))
    assert (right.x_axis, right.y_axis) == ((0, 0, -1), (0, 1, 0))
    f = g.frame_for_plane((0, 1, 0), (5, 20, 7))
    assert f.origin == (0, 20, 0) and g.to_sketch(f, g.to_model(f, 3, 4, 5)) == pytest.approx((3, 4, 5))


def test_profile_rectangle_and_hole() -> None:
    segs = _poly([(0, 0), (10, 0), (10, 5), (0, 5)])
    segs.append(SketchSegment(handle="ent:sk:9", type="circle", center=(5, 2.5), radius_mm=1))
    rep = g.analyze_profile(segs)
    assert rep.usable and len(rep.loops) == 2
    assert rep.area == pytest.approx(50 - math.pi)
    assert rep.centroid == pytest.approx((5, 2.5))
    assert rep.contains((1, 1)) and not rep.contains((5, 2.5)) and not rep.contains((11, 1))


def test_profile_with_arc_exact_area() -> None:
    segs = [
        _line(1, (-1, 0), (1, 0)),
        SketchSegment(
            handle="ent:sk:2", type="arc", start=(1, 0), end=(-1, 0), center=(0, 0), radius_mm=1, ccw=True
        ),
    ]
    rep = g.analyze_profile(segs)
    assert rep.usable and rep.area == pytest.approx(math.pi / 2, rel=1e-12)
    assert rep.centroid == pytest.approx((0, 4 / (3 * math.pi)), abs=1e-12)


def test_profile_open_crossing_duplicate() -> None:
    open_rep = g.analyze_profile(_poly([(0, 0), (10, 0), (10, 5), (0, 5)])[:3])
    assert not open_rep.closed and len(open_rep.open_points) == 2 and "extrémité libre" in open_rep.issues[0]
    bow = g.analyze_profile(_poly([(0, 0), (10, 10), (10, 0), (0, 10)]))
    assert bow.closed and bow.intersections and not bow.usable
    dup = _poly([(0, 0), (10, 0), (10, 5), (0, 5)])
    dup.append(_line(8, (10, 0), (0, 0)))
    assert g.analyze_profile(dup).duplicates
    assert not g.analyze_profile([]).closed
    construction = _poly([(0, 0), (10, 0), (10, 5), (0, 5)])
    construction.append(
        SketchSegment(handle="ent:sk:7", type="line", start=(-5, -5), end=(20, 20), construction=True)
    )
    assert g.analyze_profile(construction).usable


def test_parse_axis() -> None:
    assert (
        g.parse_axis("x") == (1, 0, 0)
        and g.parse_axis("-Z") == (0, 0, -1)
        and g.parse_axis("ent:edge:1") is None
    )
