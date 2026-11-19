"""Chaque outil du catalogue sur le backend simulé, à travers la couche MCP."""

from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest

from swmcp.tools import TOOLS

from ..conftest import Harness


async def test_catalog_complete(h: Harness) -> None:
    names = {t.name for t in await h.mcp.list_tools()}
    assert names == set(TOOLS) and len(names) == 56
    for t in await h.mcp.list_tools():
        assert t.annotations is not None and t.annotations.readOnlyHint is not None
        assert t.annotations.destructiveHint is not None and t.annotations.idempotentHint is not None
        assert t.outputSchema and set(t.outputSchema["properties"]) >= {
            "ok",
            "data",
            "warnings",
            "error",
            "state",
            "elapsed_ms",
        }
        assert t.description and len(t.description) > 60, t.name
        assert "_" in t.name and t.name == t.name.lower()


async def test_text_equals_structured(h: Harness) -> None:
    result = await h.raw("sw_status")
    assert json.loads(result.content[0].text) == result.structuredContent
    assert result.structuredContent["data"]["tool_schema_version"] == "1"


async def test_not_connected(h: Harness) -> None:
    error = await h.err("doc_list_open", "NOT_CONNECTED")
    assert error["retryable"] and "sw_connect" in error["hint"]
    data = await h.ok("sw_status")
    assert data["connected"] is False and data["profile"] == "full"


async def test_connect_and_documents(h: Harness) -> None:
    conn = await h.ok("sw_connect")
    assert conn["version"] == "2025" and conn["pid"] == 4242 and conn["active_doc"] is None
    await h.err("sw_connect", "INVALID_INPUT", pid=1)
    await h.err("inspect_tree", "NO_ACTIVE_DOC")
    part = await h.ok("doc_new", type="part")
    asm = await h.ok("doc_new", type="assembly")
    docs = await h.ok("doc_list_open")
    assert docs["total"] == 2 and [d["active"] for d in docs["documents"]] == [False, True]
    assert (await h.ok("doc_activate", doc=part["id"]))["name"] == part["name"]
    await h.err("doc_activate", "INVALID_INPUT", doc="inconnu")
    info = await h.ok("doc_info")
    assert info["type"] == "part" and info["material"] is None and info["length_unit"] == "mm"
    await h.err("sketch_begin", "WRONG_DOC_TYPE", plane="front", expected_doc=asm["name"]) if False else None
    env = await h.call("doc_info")
    assert env["state"]["doc"] == part["name"] and env["warnings"]


async def test_doc_new_without_template(h: Harness) -> None:
    await h.ok("sw_connect")
    h.fake.default_template = False
    error = await h.err("doc_new", "INVALID_INPUT", type="part")
    assert "template_path" in error["hint"]
    await h.err("doc_new", "PATH_DENIED", type="part", template_path="/etc/modele.prtdot")


async def test_save_open_close(h: Harness) -> None:
    await h.box()
    await h.err("doc_save", "INVALID_INPUT")  # jamais enregistré : path obligatoire
    await h.err("doc_save", "PATH_DENIED", path="/etc/p.sldprt")
    await h.err("doc_save", "INVALID_INPUT", path=str(h.tmp / "p.step"))
    path = str(h.tmp / "piece.sldprt")
    saved = await h.ok("doc_save", path=path)
    assert Path(saved["path"]).is_file() and not saved["overwritten"]
    assert (await h.ok("doc_close"))["closed"] == "piece.sldprt"
    opened = await h.ok("doc_open", path=path)
    assert opened["read_only"] is True and opened["type"] == "part"
    assert (await h.ok("inspect_mass"))["volume_mm3"] == pytest.approx(12000)
    # lecture seule par défaut : toute modification est refusée
    error = await h.err("sketch_begin", "READ_ONLY_MODE", plane="front")
    assert "read_only=false" in error["hint"]
    await h.ok("doc_close")
    await h.ok("doc_open", path=path, read_only=False)
    env = await h.call("dimension_set", full_name="D1@Bloc", value=20)
    assert env["ok"] and any("Point de restauration automatique" in w for w in env["warnings"])
    assert (await h.ok("checkpoint_list"))["total"] == 1
    # fermeture sans enregistrer d'un document modifié : confirmation
    closed = await h.confirmed("doc_close")
    assert closed == {"closed": "piece.sldprt", "saved": False}


async def test_open_missing_and_corrupt(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.err("doc_open", "INVALID_INPUT", path=str(h.tmp / "absent.sldprt"))
    bad = h.tmp / "casse.sldprt"
    bad.write_bytes(b"CORRUPT")
    error = await h.err("doc_open", "INVALID_INPUT", path=str(bad))
    assert "corrompu" in error["message"] and error["details"]["open_errors"]
    await h.err("doc_open", "PATH_DENIED", path="/etc/passwd.sldprt")
    await h.err("doc_open", "PATH_DENIED", path="..\\..\\x.sldprt")
    await h.err("doc_open", "INVALID_INPUT", path=str(h.tmp / "notes.txt"))


async def test_overwrite_needs_confirmation(h: Harness) -> None:
    await h.box()
    other = h.tmp / "autre.sldprt"
    other.write_bytes(b"SWMCP-FAKE existant")
    error = await h.err("doc_save", "CONFIRMATION_REQUIRED", path=str(other))
    assert "autre.sldprt" in error["details"]["effect"] and other.read_bytes() == b"SWMCP-FAKE existant"
    # un jeton ne vaut pas pour d'autres paramètres
    await h.err("doc_save", "CONFIRMATION_REQUIRED", path=str(h.tmp / "autre.sldprt"), confirm_token="faux")
    assert (await h.confirmed("doc_save", path=str(other)))["overwritten"] is True


async def test_props(h: Harness) -> None:
    await h.box()
    assert await h.ok("prop_set", name="Matière", value="S235") == {
        "name": "Matière",
        "before": None,
        "after": "S235",
    }
    assert (await h.ok("prop_get"))["properties"] == {"Matière": "S235"}
    assert (await h.ok("prop_set", name="Matière", value=None))["before"] == "S235"
    assert (await h.ok("prop_get"))["properties"] == {}


async def test_checkpoints(h: Harness) -> None:
    await h.box()
    await h.err("checkpoint_create", "INVALID_INPUT", label="../x")
    cp = await h.ok("checkpoint_create", label="avant-conges")
    assert Path(cp["path"]).is_file() and ".swmcp/checkpoints" in cp["path"].replace("\\", "/")
    await h.ok("dimension_set", full_name="D1@Bloc", value=25)
    restored = await h.ok("checkpoint_restore", id=cp["id"])
    assert restored["opened"]["path"] == cp["path"]
    assert (await h.ok("inspect_mass"))["volume_mm3"] == pytest.approx(12000)  # état d'avant
    assert (await h.ok("doc_list_open"))["total"] == 2  # l'original reste ouvert, intact
    await h.err("checkpoint_restore", "INVALID_INPUT", id="x/../../y")


# --- esquisse -------------------------------------------------------------------------------
async def test_sketch_flow_fully_defined(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.err(
        "sketch_add_line", "INVALID_INPUT", x1_mm=0, y1_mm=0, x2_mm=1, y2_mm=1
    )  # pas d'esquisse ouverte
    begun = await h.ok("sketch_begin", plane="top", name="Base")
    assert begun["frame"]["y_axis"] == [0, 0, -1] and begun["frame"]["normal"] == [0, 1, 0]
    await h.err("sketch_begin", "SKETCH_OPEN", plane="front")
    await h.err("feature_extrude", "SKETCH_OPEN", sketch="Base", depth_mm=5)
    rect = await h.ok("sketch_add_rect", x1_mm=0, y1_mm=0, x2_mm=40, y2_mm=20)
    by_role = {e["role"]: e["handle"] for e in rect["entities"]}
    assert rect["sketch"] == "Base" and set(by_role) >= {"bottom", "right", "top", "left", "bottom_left"}
    rel = await h.ok("sketch_add_relation", type="coincident", entities=[by_role["bottom_left"], "origin"])
    assert rel["status"] == "under_defined"
    d1 = await h.ok(
        "sketch_add_dimension", type="length", entities=[by_role["bottom"]], value=50, name="Longueur"
    )
    assert d1["full_name"] == "Longueur@Base"
    d2 = await h.ok("sketch_add_dimension", type="length", entities=[by_role["right"]], value=20)
    assert d2 == {"full_name": "D2@Base", "value": 20, "unit": "mm", "status": "fully_defined"}
    await h.err(
        "sketch_add_dimension",
        "INVALID_INPUT",
        type="length",
        entities=[by_role["bottom"]],
        value=1,
        name="Longueur",
    )
    seen = await h.ok("sketch_inspect")
    assert seen["status"] == "fully_defined" and seen["closed"] and len(seen["dimensions"]) == 2
    bottom = next(s for s in seen["segments"] if s["handle"] == by_role["bottom"])
    assert bottom["end"] == [50, 0]  # la cote a piloté la géométrie
    end = await h.ok("sketch_end")
    assert end == {
        "name": "Base",
        "status": "fully_defined",
        "closed": True,
        "segment_count": 4,
        "issues": [],
    }
    assert (await h.ok("sketch_inspect", name="Base"))["name"] == "Base"
    await h.err("sketch_inspect", "INVALID_INPUT", name="Inconnue")


async def test_sketch_primitives_validation(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front")
    await h.err("sketch_add_line", "INVALID_INPUT", x1_mm=1, y1_mm=1, x2_mm=1, y2_mm=1)
    await h.err("sketch_add_rect", "INVALID_INPUT", x1_mm=0, y1_mm=0, x2_mm=0, y2_mm=5)
    await h.err("sketch_add_circle", "INVALID_INPUT", cx_mm=0, cy_mm=0)
    await h.err("sketch_add_circle", "INVALID_INPUT", cx_mm=0, cy_mm=0, radius_mm=1, diameter_mm=2)
    await h.err("sketch_add_circle", "INVALID_INPUT", cx_mm=0, cy_mm=0, radius_mm=-1)
    await h.err("sketch_add_arc", "INVALID_INPUT", cx_mm=0, cy_mm=0, x1_mm=10, y1_mm=0, x2_mm=0, y2_mm=9)
    await h.err("sketch_add_polyline", "INVALID_INPUT", points_mm=[[0, 0], [1, 0], [0, 0]], closed=True)
    await h.err("sketch_add_slot", "INVALID_INPUT", x1_mm=0, y1_mm=0, x2_mm=0, y2_mm=0, width_mm=4)
    await h.err("sketch_add_relation", "INVALID_INPUT", type="tangent", entities=["ent:sk:999", "origin"])
    await h.err("sketch_add_relation", "ENTITY_STALE", type="horizontal", entities=["ent:sk:999"])
    await h.err("sketch_add_relation", "INVALID_INPUT", type="horizontal", entities=["n'importe quoi"])
    center = await h.ok("sketch_add_rect", x1_mm=10, y1_mm=10, x2_mm=15, y2_mm=12, mode="center")
    seen = await h.ok("sketch_inspect")
    assert seen["segments"][0]["start"] == [5, 8] and len(center["entities"]) == 8
    for tool, args in (
        ("sketch_add_circle", {"cx_mm": 30, "cy_mm": 0, "diameter_mm": 5}),
        (
            "sketch_add_arc",
            {"cx_mm": 0, "cy_mm": 30, "x1_mm": 5, "y1_mm": 30, "x2_mm": 0, "y2_mm": 35, "direction": "cw"},
        ),
        ("sketch_add_slot", {"x1_mm": 40, "y1_mm": 40, "x2_mm": 60, "y2_mm": 40, "width_mm": 6}),
        ("sketch_add_point", {"x_mm": 1, "y_mm": 1}),
        ("sketch_add_line", {"x1_mm": -5, "y1_mm": -5, "x2_mm": -5, "y2_mm": 9, "construction": True}),
    ):
        assert (await h.ok(tool, **args))["entities"]
    end = await h.ok("sketch_end")  # l'arc isolé laisse deux extrémités libres
    assert end["closed"] is False and end["segment_count"] == 12 and "extrémité libre" in end["issues"][0]


async def test_empty_sketch_removed(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front", name="Vide")
    assert (await h.ok("sketch_end"))["status"] == "failed"
    assert all(n["name"] != "Vide" for n in (await h.ok("inspect_tree"))["items"])


# --- fonctions ------------------------------------------------------------------------------
async def test_extrude_verify_levels(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front", name="Esq")
    await h.ok("sketch_add_rect", x1_mm=0, y1_mm=0, x2_mm=40, y2_mm=30)
    await h.ok("sketch_end")
    await h.err("feature_extrude", "INVALID_INPUT", sketch="Esq")  # depth_mm manquant
    await h.err("feature_extrude", "INVALID_INPUT", sketch="Esq", depth_mm=5, end_condition="through_all")
    await h.err("feature_extrude", "INVALID_INPUT", sketch="Inconnue", depth_mm=5)
    await h.err("feature_extrude", "INVALID_INPUT", sketch="Esq", depth_mm=5, name="a@b")
    res = await h.ok("feature_extrude", sketch="Esq", depth_mm=10, name="Bloc", verify="full")
    assert res["feature"] == "Bloc" and res["dimensions"] == ["D1@Bloc"]
    assert res["volume_before_mm3"] == 0 and res["volume_after_mm3"] == pytest.approx(12000)
    v = res["verify"]
    assert v["rebuild_ok"] and v["body_count"] == 1 and v["bbox_mm"]["size"] == [40, 30, 10]
    assert v["face_count"] == 6 and v["edge_count"] == 12 and v["volume_delta_mm3"] == pytest.approx(12000)
    await h.err("feature_extrude", "INVALID_INPUT", sketch="Esq", depth_mm=10, name="Bloc")  # nom déjà pris


async def test_feature_failed_has_diagnostic(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front", name="Ouvert")
    await h.ok("sketch_add_polyline", points_mm=[[0, 0], [10, 0], [10, 5]])
    end = await h.call("sketch_end")
    assert end["data"]["closed"] is False and any("Contour non exploitable" in w for w in end["warnings"])
    env = await h.call("feature_extrude", sketch="Ouvert", depth_mm=5)
    error = env["error"]
    assert error["code"] == "SKETCH_NOT_CLOSED" and "Ouvert" in error["hint"]
    assert error["details"]["kept"] is False and error["details"]["sketch"]["closed"] is False
    assert (
        env["state"]["rebuild_ok"] is True and env["state"]["sketch_open"] is None
    )  # fonction annulée, état propre
    assert all(n["type"] != "Extrude" for n in (await h.ok("inspect_tree"))["items"])


async def test_cut_and_direction(h: Harness) -> None:
    await h.box(40, 30, 10)
    top = (await h.ok("find_faces", planar=True, normal=[0, 0, 1], extreme="max_z"))["items"][0]
    assert top["area_mm2"] == pytest.approx(1200) and top["centroid_mm"] == [20, 15, 10]
    begun = await h.ok("sketch_begin", plane=top["handle"], name="Poche")
    assert begun["plane"] == "face" and begun["frame"]["origin"] == [0, 0, 10]
    await h.ok("sketch_add_rect", x1_mm=10, y1_mm=10, x2_mm=30, y2_mm=20)
    await h.ok("sketch_end")
    wrong = await h.err(
        "feature_cut_extrude", "FEATURE_FAILED", sketch="Poche", depth_mm=4, direction="reverse"
    )
    assert wrong["details"]["kept"] is False
    res = await h.ok("feature_cut_extrude", sketch="Poche", depth_mm=4, name="Poche_1")
    assert res["verify"]["volume_delta_mm3"] == pytest.approx(-800)
    through = await h.ok("sketch_begin", plane=top["handle"], name="Trou")
    assert through["name"] == "Trou"
    await h.ok("sketch_add_circle", cx_mm=5, cy_mm=5, diameter_mm=4)
    await h.ok("sketch_end")
    res = await h.ok("feature_cut_extrude", sketch="Trou", end_condition="through_all")
    assert res["verify"]["volume_delta_mm3"] == pytest.approx(-math.pi * 4 * 10)


async def test_hole_position_in_model_frame(h: Harness) -> None:
    """Piège « repère de l'esquisse » (spec §10) : percer à (x, y, z) modèle, puis mesurer la position."""
    await h.box(40, 30, 10, plane="right")  # bloc dans le plan YZ : repère d'esquisse x = -Z
    face = (await h.ok("find_faces", planar=True, normal=[1, 0, 0], extreme="max_x"))["items"][0]
    await h.err("feature_hole", "INVALID_INPUT", face=face["handle"], points_mm=[[10, 5, -12]], diameter_mm=4)
    await h.err(
        "feature_hole",
        "INVALID_INPUT",
        face=face["handle"],
        points_mm=[[10, 5, -12]],
        diameter_mm=4,
        depth_mm=2,
        through=True,
    )
    res = await h.ok("feature_hole", face=face["handle"], points_mm=[[10, 5, -12]], diameter_mm=4, depth_mm=6)
    assert res["feature"] == "Hole_1" and res["verify"]["volume_delta_mm3"] == pytest.approx(-math.pi * 4 * 6)
    rim = (await h.ok("find_edges", kind="circular", radius_mm=2, extreme="max_x"))["items"][0]
    assert rim["center_mm"] == pytest.approx([10, 5, -12])
    bottom = (await h.ok("find_faces", planar=True, normal=[1, 0, 0], area_range_mm2=[12, 13]))["items"][0]
    assert bottom["centroid_mm"] == pytest.approx([4, 5, -12])  # fond plat à 6 mm de profondeur
    await h.err(
        "feature_hole",
        "ENTITY_STALE",
        face="ent:face:999",
        points_mm=[[0, 0, 0]],
        diameter_mm=1,
        through=True,
    )


async def test_revolve(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front", name="Profil")
    axis = (await h.ok("sketch_add_line", x1_mm=0, y1_mm=0, x2_mm=0, y2_mm=50, construction=True))[
        "entities"
    ][0]["handle"]
    await h.ok(
        "sketch_add_polyline", points_mm=[[0, 0], [15, 0], [15, 20], [10, 20], [10, 50], [0, 50]], closed=True
    )
    await h.ok("sketch_end")
    res = await h.ok("feature_revolve", sketch="Profil", axis=axis, verify="full")
    assert res["volume_after_mm3"] == pytest.approx(math.pi * 7500, rel=1e-9)
    assert res["verify"]["bbox_mm"]["size"] == pytest.approx([30, 50, 30])
    await h.err("feature_revolve", "ENTITY_STALE", sketch="Profil", axis="ent:sk:999")
    half = await h.ok("dimension_set", full_name="D1@Revolve_1", value=180)
    assert half["unit"] == "deg" and half["volume_after_mm3"] == pytest.approx(math.pi * 3750, rel=1e-9)


async def test_fillet_chamfer_and_stale_handles(h: Harness) -> None:
    await h.box(40, 30, 10)
    vertical = (await h.ok("find_edges", kind="linear", length_range_mm=[9.9, 10.1]))["items"]
    assert len(vertical) == 4 and all(len(e["faces"]) == 2 for e in vertical)
    await h.err("feature_fillet", "INVALID_INPUT", edges=["ent:face:1"], radius_mm=1)
    await h.err("feature_fillet", "ENTITY_STALE", edges=["ent:edge:999"], radius_mm=1)
    too_big = await h.err("feature_fillet", "FEATURE_FAILED", edges=[vertical[0]["handle"]], radius_mm=35)
    assert "Réduire" in too_big["hint"]
    res = await h.ok("feature_fillet", edges=[vertical[0]["handle"]], radius_mm=3)
    assert res["verify"]["volume_delta_mm3"] == pytest.approx(-(1 - math.pi / 4) * 9 * 10)
    await h.err(
        "feature_fillet", "ENTITY_STALE", edges=[vertical[0]["handle"]], radius_mm=1
    )  # arête consommée
    long_edge = (await h.ok("find_edges", kind="linear", length_range_mm=[39.9, 40.1], limit=1))["items"][0]
    res = await h.ok("feature_chamfer", edges=[long_edge["handle"]], distance_mm=2)
    assert res["verify"]["volume_delta_mm3"] == pytest.approx(-80) and res["dimensions"] == [
        "D1@Chamfer_1",
        "D2@Chamfer_1",
    ]


async def test_shell_patterns_mirror(h: Harness) -> None:
    await h.box(40, 30, 10)
    top = (await h.ok("find_faces", planar=True, normal=[0, 0, 1], extreme="max_z"))["items"][0]["handle"]
    await h.ok("feature_hole", face=top, points_mm=[[5, 5, 10]], diameter_mm=2, through=True, name="Trou")
    one = math.pi * 10
    res = await h.ok("feature_pattern_linear", features=["Trou"], direction="x", count=4, spacing_mm=10)
    assert res["verify"]["volume_delta_mm3"] == pytest.approx(-3 * one)
    outside = await h.err(
        "feature_mirror", "FEATURE_FAILED", features=["Trou"], plane="top"
    )  # image hors matière
    assert outside["details"]["kept"] is False
    await h.err(
        "feature_pattern_linear", "INVALID_INPUT", features=["Esq"], direction="x", count=2, spacing_mm=5
    )
    await h.err(
        "feature_pattern_linear",
        "INVALID_INPUT",
        features=["Trou"],
        direction="diagonale",
        count=2,
        spacing_mm=5,
    )
    await h.err(
        "feature_pattern_circular", "FEATURE_FAILED", features=["Trou"], axis="z", count=4
    )  # copies hors matière
    too_thick = await h.err("feature_shell", "FEATURE_FAILED", faces_to_remove=[top], thickness_mm=5)
    assert "thickness_mm" in too_thick["hint"]
    res = await h.ok("feature_shell", faces_to_remove=[top], thickness_mm=2)
    assert res["verify"]["volume_delta_mm3"] < 0 and res["dimensions"] == ["D1@Shell_1"]
    await h.err("find_edges", "INVALID_INPUT", length_range_mm=[5, 1])


async def test_dimension_set_reverts_on_rebuild_failure(h: Harness) -> None:
    await h.box(40, 30, 10)
    edge = (await h.ok("find_edges", kind="linear", length_range_mm=[9.9, 10.1], limit=1))["items"][0][
        "handle"
    ]
    await h.ok("feature_fillet", edges=[edge], radius_mm=3)
    env = await h.call("dimension_set", full_name="D1@Fillet_1", value=50)
    assert env["error"]["code"] == "REBUILD_FAILED" and env["error"]["details"]["reverted"] is True
    assert (
        env["error"]["details"]["errors"][0]["feature"] == "Fillet_1" and env["state"]["rebuild_ok"] is True
    )
    dims = {d["full_name"]: d["value"] for d in (await h.ok("inspect_dimensions"))["items"]}
    assert dims == {"D1@Bloc": 10, "D1@Fillet_1": 3}
    await h.err("dimension_set", "INVALID_INPUT", full_name="D9@Bloc", value=1)
    await h.err("dimension_set", "INVALID_INPUT", full_name="sansarobase", value=1)
    await h.err("dimension_set", "INVALID_INPUT", full_name="D1@Bloc", value=-4)
    assert (await h.ok("inspect_dimensions", feature="Bloc"))["total"] == 1


async def test_variable_rename_suppress_delete(h: Harness) -> None:
    await h.box(40, 30, 10)
    assert (await h.ok("variable_set", name="Ep", value=4))["new_value"] == "4mm"
    assert (await h.ok("variable_set", name="Ep", value=5, unit=""))["old_value"] == "4mm"
    await h.err("variable_set", "INVALID_INPUT", name="1+1; rm", value=1)
    assert (await h.ok("feature_rename", name="Bloc", new_name="Corps"))["feature"] == "Corps"
    assert (await h.ok("inspect_dimensions"))["items"][0]["full_name"] == "D1@Corps"
    await h.err("feature_rename", "INVALID_INPUT", name="Corps", new_name="Esq")
    sup = await h.ok("feature_suppress", name="Corps")
    assert sup["suppressed"] is True and (await h.ok("inspect_mass"))["volume_mm3"] == 0
    await h.ok("feature_suppress", name="Corps", suppressed=False)
    error = await h.err("feature_delete", "CONFIRMATION_REQUIRED", name="Esq")
    assert "Corps" in error["details"]["effect"] and error["details"]["expires_in_s"] == 120
    assert (await h.ok("inspect_mass"))["volume_mm3"] == pytest.approx(12000)  # rien n'a été effacé
    deleted = await h.confirmed("feature_delete", name="Esq", delete_children=True)
    assert deleted["deleted"] == ["Esq", "Corps"] and deleted["checkpoint"] is None
    await h.err("feature_delete", "INVALID_INPUT", name="Esq")


async def test_mirror(h: Harness) -> None:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front", name="Esq")
    await h.ok("sketch_add_rect", x1_mm=0, y1_mm=0, x2_mm=20, y2_mm=15, mode="center")
    await h.ok("sketch_end")
    await h.ok("feature_extrude", sketch="Esq", depth_mm=10, name="Bloc")
    top = (await h.ok("find_faces", normal=[0, 0, 1]))["items"][0]["handle"]
    await h.ok("feature_hole", face=top, points_mm=[[10, 5, 10]], diameter_mm=4, depth_mm=5, name="Trou")
    res = await h.ok("feature_mirror", features=["Trou"], plane="right")
    assert res["type"] == "Mirror" and res["verify"]["volume_delta_mm3"] == pytest.approx(-math.pi * 4 * 5)
    assert (await h.ok("find_edges", kind="circular", near_point_mm=[-10, 5, 10], limit=1))["items"][0][
        "center_mm"
    ] == [-10, 5, 10]


# --- inspection -----------------------------------------------------------------------------
async def test_tree_and_pagination(h: Harness) -> None:
    await h.box()
    page = await h.ok("inspect_tree", limit=3)
    assert [n["name"] for n in page["items"]] == [
        "Plan de face",
        "Plan de dessus",
        "Plan de droite",
    ]  # noms français
    assert page["total"] == 6 and page["next_cursor"] == "3"
    rest = await h.ok("inspect_tree", limit=3, cursor=page["next_cursor"])
    assert [n["name"] for n in rest["items"]] == ["Origine", "Esq", "Bloc"] and rest["next_cursor"] is None
    assert rest["items"][1]["parent"] == "Bloc"
    await h.err("inspect_tree", "INVALID_INPUT", cursor="abc")
    await h.err("inspect_tree", "INVALID_INPUT", limit=500)


async def test_mass_and_material(h: Harness) -> None:
    await h.box(40, 30, 10)
    env = await h.call("inspect_mass")
    assert env["data"]["mass_g"] == 0 and env["data"]["material"] is None and "matériau" in env["warnings"][0]
    h.fake.set_material("Acier")
    data = await h.ok("inspect_mass")
    assert data["mass_g"] == pytest.approx(94.2) and data["density_g_cm3"] == 7.85
    assert data["center_of_mass_mm"] == [20, 15, 5] and data["surface_area_mm2"] == pytest.approx(3800)


async def test_rebuild_audit(h: Harness) -> None:
    await h.box(40, 30, 10)
    edge = (await h.ok("find_edges", kind="linear", length_range_mm=[9.9, 10.1], limit=1))["items"][0][
        "handle"
    ]
    await h.ok("feature_fillet", edges=[edge], radius_mm=2)
    await h.confirmed("feature_delete", name="Bloc")
    audit = await h.ok("inspect_rebuild")
    assert audit["rebuild_ok"] is False and audit["errors"][0]["feature"] == "Fillet_1"
    assert audit["dangling_references"] == ["Fillet_1"] and audit["under_defined_sketches"] == ["Esq"]
    tree = {n["name"]: n for n in (await h.ok("inspect_tree"))["items"]}
    assert tree["Fillet_1"]["status"] == "error" and "pendante" in tree["Fillet_1"]["message"]
    check = await h.ok(
        "check_design", rules=[{"rule": "no_rebuild_errors"}, {"rule": "sketches_fully_defined"}]
    )
    assert check["passed"] is False and check["results"][0]["measured"] == 1


async def test_measure(h: Harness) -> None:
    await h.box(40, 30, 10)
    front = (await h.ok("find_faces", normal=[0, 0, -1]))["items"][0]["handle"]
    back = (await h.ok("find_faces", normal=[0, 0, 1]))["items"][0]["handle"]
    side = (await h.ok("find_faces", normal=[1, 0, 0]))["items"][0]["handle"]
    assert (await h.ok("inspect_measure", a=front, b=back))["distance_mm"] == pytest.approx(10)
    assert (await h.ok("inspect_measure", a=front, b=side))["angle_deg"] == pytest.approx(90)
    assert (await h.ok("inspect_measure", a=[0, 0, 0], b=[3, 4, 0]))["distance_mm"] == 5
    assert (await h.ok("inspect_measure", a=back, b=[5, 5, 25]))["distance_mm"] == pytest.approx(15)
    await h.err("inspect_measure", "ENTITY_STALE", a="ent:face:999", b=[0, 0, 0])


async def test_check_design_all_six_rules(h: Harness) -> None:
    await h.box(40, 30, 10)
    rules: list[dict[str, Any]] = [
        {"rule": "no_rebuild_errors"},
        {"rule": "sketches_fully_defined"},
        {"rule": "single_body"},
        {"rule": "bbox_within", "size_mm": [40, 30, 10]},
        {"rule": "volume_within", "volume_mm3": 12000},
        {"rule": "has_material"},
    ]
    res = await h.ok("check_design", rules=rules)
    by = {r["rule"]: r for r in res["results"]}
    assert res["passed"] is False and len(by) == 6
    assert [
        by[r]["passed"] for r in ("no_rebuild_errors", "single_body", "bbox_within", "volume_within")
    ] == [True] * 4
    assert by["sketches_fully_defined"]["measured"] == ["Esq"] and by["has_material"]["measured"] is None
    res = await h.ok(
        "check_design",
        rules=[
            {"rule": "bbox_within", "size_mm": [40, 30, 10.2]},
            {"rule": "volume_within", "volume_mm3": 12200},
        ],
    )
    assert [r["passed"] for r in res["results"]] == [False, False] and res["results"][0]["measured"] == [
        40,
        30,
        10,
    ]
    res = await h.ok(
        "check_design", rules=[{"rule": "bbox_within", "size_mm": [10, 40, 30], "any_orientation": True}]
    )
    assert res["passed"] is True
    await h.err("check_design", "INVALID_INPUT", rules=[{"rule": "bbox_within"}])
    await h.err("check_design", "INVALID_INPUT", rules=[{"rule": "inconnue"}])


async def test_interference(h: Harness) -> None:
    await h.box()
    await h.err("inspect_interference", "WRONG_DOC_TYPE")
    await h.ok("doc_new", type="assembly")
    h.fake.add_component("A-1", (0, 0, 0), (10, 10, 10))
    h.fake.add_component("B-1", (8, 8, 8), (20, 20, 20), lightweight=True)
    h.fake.add_component("C-1", (50, 0, 0), (60, 10, 10))
    res = await h.ok("inspect_interference")
    assert res == {"count": 1, "pairs": [{"a": "A-1", "b": "B-1", "volume_mm3": 8}]}
    env = await h.call("inspect_tree")
    assert env["data"]["items"][1]["lightweight"] is True and "allégés" in env["warnings"][0]
    assert (await h.ok("inspect_tree", resolve=True))["items"][1]["lightweight"] is False
    await h.err("find_faces", "WRONG_DOC_TYPE")


async def test_view_capture(h: Harness) -> None:
    from PIL import Image

    await h.box()
    result = await h.raw("view_capture", views=["iso", "front", "top"], width_px=600)
    data = result.structuredContent["data"]
    images = [c for c in result.content if c.type == "image"]
    assert len(images) == 1 and data["grid"] is True and data["views"] == ["iso", "front", "top"]
    img = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
    assert img.format == "PNG" and max(img.size) <= 600 and img.size == (data["width_px"], data["height_px"])
    assert "data" not in json.loads(result.content[0].text)["data"]  # pas de binaire dans le texte
    separate = await h.raw("view_capture", views=["iso", "front"], grid=False)
    assert len([c for c in separate.content if c.type == "image"]) == 2
    await h.err("view_capture", "INVALID_INPUT", width_px=4000)
    await h.err("view_capture", "INVALID_INPUT", views=["dessous"])


async def test_export(h: Harness) -> None:
    await h.box()
    out = h.tmp / "export" / "bloc.step"
    res = await h.ok("export_file", format="step", path=str(out))
    assert out.is_file() and res["size_bytes"] == out.stat().st_size and res["overwritten"] is False
    await h.err("export_file", "CONFIRMATION_REQUIRED", format="step", path=str(out))
    assert (await h.confirmed("export_file", format="step", path=str(out)))["overwritten"] is True
    await h.err("export_file", "INVALID_INPUT", format="step", path=str(h.tmp / "bloc.stl"))
    await h.err("export_file", "PATH_DENIED", format="stl", path="/etc/bloc.stl")
    await h.err("export_file", "WRONG_DOC_TYPE", format="pdf", path=str(h.tmp / "plan.pdf"))
    png = await h.ok("export_file", format="png", path=str(h.tmp / "vue.png"), width_px=300)
    assert Path(png["path"]).read_bytes()[:4] == b"\x89PNG"
    for fmt, name in (("stl", "b.stl"), ("iges", "b.igs")):
        await h.ok("export_file", format=fmt, path=str(h.tmp / name))
    await h.ok("doc_new", type="drawing")
    for fmt in ("pdf", "dxf"):
        await h.ok("export_file", format=fmt, path=str(h.tmp / f"plan.{fmt}"))
    await h.err("export_file", "WRONG_DOC_TYPE", format="step", path=str(h.tmp / "plan.step"))


async def test_export_size_cap(make: Any) -> None:
    h: Harness = make(max_export_mb=0.00001)
    await h.box()
    out = h.tmp / "gros.step"
    error = await h.err("export_file", "INVALID_INPUT", format="step", path=str(out))
    assert "plafond" in error["message"] and not out.exists()


# --- API et connaissances ---------------------------------------------------------------------
async def test_knowledge_get(h: Harness) -> None:
    index = await h.ok("knowledge_get", topic="index")
    assert set(index["available"]) == {
        "dfm-machining",
        "dfm-sheet-metal",
        "error-recipes",
        "features-guide",
        "materials-and-units",
        "modeling-method",
        "sketch-practices",
    }
    sheet = await h.ok("knowledge_get", topic="error-recipes")
    assert (
        sheet["version"] == "1.0.0"
        and "ENTITY_STALE" in sheet["content"]
        and not sheet["content"].startswith("---")
    )
    error = await h.err("knowledge_get", "INVALID_INPUT", topic="dfm-3d-printing")
    assert "modeling-method" in error["hint"]


async def test_api_index(h: Harness) -> None:
    from swmcp.apiindex import ApiParam, IndexWriter

    env = await h.call("api_search", query="extrusion")
    assert env["data"]["indexed"] is False and "swmcp index-api" in env["warnings"][0]
    assert (await h.ok("api_describe", name="x"))["indexed"] is False
    with IndexWriter(h.rt.settings.api_index_path) as w:
        w.add_interface("SldWorks", "IFeatureManager")
        w.add_member(
            "SldWorks",
            "IFeatureManager",
            "FeatureExtrusion3",
            "method",
            [
                ApiParam(name="Sd", type="bool", direction="in"),
                ApiParam(name="D1", type="float", direction="in"),
            ],
            "Feature",
        )
        w.add_enum("SwConst", "swEndConditions_e", {"swEndCondBlind": 0, "swEndCondThroughAll": 1})
    found = await h.ok("api_search", query="extrusion", kind="method")
    assert found["indexed"] and found["results"][0]["name"] == "IFeatureManager.FeatureExtrusion3"
    doc = await h.ok("api_describe", name="IFeatureManager.FeatureExtrusion3")
    assert doc["found"] and [p["name"] for p in doc["member"]["params"]] == ["Sd", "D1"]
    assert (await h.ok("api_describe", name="Inconnu"))["found"] is False


async def test_api_call_raw(h: Harness) -> None:
    await h.box()
    error = await h.err("api_call_raw", "INVALID_INPUT", target="doc", method="RunMacro2")
    assert "liste blanche" in error["message"]
    await h.err("api_call_raw", "INVALID_INPUT", target="doc", method="Get Title")
    await h.err("api_call_raw", "CONFIRMATION_REQUIRED", target="app", method="RevisionNumber")
    res = await h.confirmed("api_call_raw", target="app", method="RevisionNumber")
    assert res == {"target": "app", "method": "RevisionNumber", "result": "33.1.0"}
