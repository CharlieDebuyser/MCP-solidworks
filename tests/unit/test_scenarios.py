"""Scénarios d'acceptation de la spec §2, joués sur le backend simulé, et surfaces MCP annexes."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from swmcp import cli, knowledge
from swmcp.selftest import run_selftest

from ..conftest import Harness, make_settings

L_VOLUME = 291 * 30 - 2 * math.pi * 6.25 * 3


async def build_bracket(h: Harness) -> dict[str, str]:
    await h.ok("sw_connect")
    await h.ok("doc_new", type="part")
    await h.ok("sketch_begin", plane="front", name="Profil")
    poly = await h.ok(
        "sketch_add_polyline", points_mm=[[0, 0], [60, 0], [60, 3], [3, 3], [3, 40], [0, 40]], closed=True
    )
    lines = [e["handle"] for e in poly["entities"] if e["type"] == "line"]
    points = [e["handle"] for e in poly["entities"] if e["type"] == "point"]
    await h.ok("sketch_add_relation", type="horizontal", entities=lines[0::2])
    await h.ok("sketch_add_relation", type="vertical", entities=lines[1::2])
    await h.ok("sketch_add_relation", type="coincident", entities=[points[0], "origin"])
    for line, value, name in (
        (lines[0], 60, "Longueur"),
        (lines[1], 3, "Epaisseur"),
        (lines[5], 40, "Hauteur"),
        (lines[4], 3, "Epaisseur2"),
    ):
        last = await h.ok("sketch_add_dimension", type="length", entities=[line], value=value, name=name)
    assert last["status"] == "fully_defined"
    await h.ok("sketch_end")
    await h.ok("feature_extrude", sketch="Profil", depth_mm=30, name="Equerre")
    wing = (await h.ok("find_faces", planar=True, normal=[0, 1, 0], near_point_mm=[50, 3, 15], limit=1))[
        "items"
    ][0]["handle"]
    hole = await h.ok(
        "feature_hole",
        face=wing,
        points_mm=[[50, 3, 10], [50, 3, 20]],
        diameter_mm=5,
        through=True,
        name="Percages",
    )
    return {"hole_sketch": hole["sketch"]}


async def test_scenario_1_creation(h: Harness) -> None:
    await build_bracket(h)
    h.fake.set_material("Acier")
    check = await h.ok(
        "check_design",
        rules=[
            {"rule": "no_rebuild_errors"},
            {"rule": "single_body"},
            {"rule": "has_material"},
            {"rule": "bbox_within", "size_mm": [60, 40, 30]},
            {"rule": "volume_within", "volume_mm3": L_VOLUME},
        ],
    )
    assert check["passed"], check
    capture = await h.raw("view_capture", views=["iso"])
    assert [c.type for c in capture.content] == ["text", "image"]


async def test_scenario_2_parametric_change(h: Harness) -> None:
    info = await build_bracket(h)
    h.fake.set_material("Acier")
    thick = await h.ok("dimension_set", full_name="Epaisseur@Profil", value=4)
    assert thick["old_value"] == 3 and thick["rebuild_ok"] and thick["mass_after_g"] > thick["mass_before_g"]
    dims = {d["full_name"]: d["value"] for d in (await h.ok("inspect_dimensions"))["items"]}
    assert dims["Epaisseur@Profil"] == 4 and dims["Longueur@Profil"] == 60
    await h.err(
        "dimension_set", "INVALID_INPUT", full_name=f"D1@{info['hole_sketch']}", value=6
    )  # cote non créée
    seen = await h.ok("sketch_inspect", name="Profil")
    assert [s["end"] for s in seen["segments"]][:3] == [[60, 0], [60, 4], [3, 4]]


async def test_scenario_3_audit(h: Harness) -> None:
    await build_bracket(h)
    edge = (await h.ok("find_edges", kind="linear", length_range_mm=[29.9, 30.1], limit=1))["items"][0][
        "handle"
    ]
    await h.ok("feature_fillet", edges=[edge], radius_mm=1, name="Conge")
    await h.confirmed("feature_delete", name="Equerre")
    report = await h.ok("inspect_rebuild")
    assert {e["feature"] for e in report["errors"]} == {"Percages", "Conge"}
    assert set(report["dangling_references"]) == {"Percages", "Conge"}
    assert report["under_defined_sketches"] == ["Percages_esq"]


async def test_scenario_4_batch_export(make: Any) -> None:
    h: Harness = make(profile="design")
    folder = h.tmp / "lot"
    folder.mkdir()
    for name in ("a.sldprt", "b.sldprt"):
        (folder / name).write_bytes(b"SWMCP-FAKE part")
    (folder / "casse.sldprt").write_bytes(b"CORRUPT")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    prompt = await h.mcp.get_prompt("batch_export", {"folder": str(folder), "format": "step"})
    text = prompt.messages[0].content.text  # type: ignore[union-attr]
    assert all(str(folder / n) in text for n in before) and "doc_open" in text
    await h.ok("sw_connect")
    report: dict[str, str] = {}
    for name in sorted(before):
        opened = await h.call("doc_open", path=str(folder / name))
        if not opened["ok"]:
            report[name] = opened["error"]["code"]
            continue
        exported = await h.call(
            "export_file", format="step", path=str(folder / "export" / (Path(name).stem + ".step"))
        )
        report[name] = "OK" if exported["ok"] else exported["error"]["code"]
        await h.ok("doc_close")
    assert report == {"a.sldprt": "OK", "b.sldprt": "OK", "casse.sldprt": "INVALID_INPUT"}
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before  # originaux intacts
    assert sorted(p.name for p in (folder / "export").iterdir()) == ["a.step", "b.step"]
    denied = await h.mcp.get_prompt("batch_export", {"folder": "/etc", "format": "step"})
    assert "n'a pas pu être lu" in denied.messages[0].content.text  # type: ignore[union-attr]


# --- ressources, prompts, instructions ----------------------------------------------------------
async def test_resources_and_prompts(h: Harness) -> None:
    resources = {str(r.uri): r for r in await h.mcp.list_resources()}
    assert len(resources) == 7 and "swmcp://knowledge/dfm-3d-printing" not in resources
    content = list(await h.mcp.read_resource("swmcp://knowledge/modeling-method"))
    assert content[0].mime_type == "text/markdown" and "données" in content[0].content
    prompts = {p.name for p in await h.mcp.list_prompts()}
    assert prompts == {"design_part", "modify_part", "audit_model", "batch_export"}
    design = await h.mcp.get_prompt("design_part", {"description": "Support en L"})
    assert (
        "Support en L" in design.messages[0].content.text
        and "check_design" in design.messages[0].content.text
    )  # type: ignore[union-attr]
    for name, args in (("modify_part", {"change": "épaisseur 4"}), ("audit_model", {})):
        assert "inspect_tree" in (await h.mcp.get_prompt(name, args)).messages[0].content.text  # type: ignore[union-attr]


def test_knowledge_sheets_format() -> None:
    sheets = knowledge.sheets()
    for sheet in sheets.values():
        assert sheet.title != sheet.topic and sheet.when_to_use and sheet.version and sheet.date, sheet.topic
        assert len(sheet.body) / 3.2 < 1500, f"{sheet.topic} dépasse ~1 500 jetons"
    assert len(knowledge.instructions()) / 3.2 < 1500
    for word in ("mm", "confirm_token", "find_faces", "knowledge_get"):
        assert word in knowledge.instructions()
    known = set(__import__("swmcp.tools", fromlist=["TOOLS"]).TOOLS)
    import re

    for sheet in [*sheets.values()]:
        for name in re.findall(
            r"`((?:sw|doc|sketch|feature|inspect|find|check|view|export|api|knowledge|prop|checkpoint|dimension|variable)_[a-z_]+)`",
            sheet.body,
        ):
            assert name in known or name.endswith("_"), f"{sheet.topic} cite un outil inconnu : {name}"


async def test_instructions_exposed_at_initialize(h: Harness) -> None:
    assert h.mcp.instructions == knowledge.instructions() and h.mcp.name == "solidworks"


# --- selftest et ligne de commande ----------------------------------------------------------------
async def test_selftest_on_fake(tmp_path: Path) -> None:
    report = await run_selftest(make_settings(tmp_path, profile="design"))
    assert report["ok"], [s for s in report["steps"] if not s["ok"]]
    assert report["backend"] == "fake" and report["passed"] == len(report["steps"]) >= 20
    assert list((tmp_path / "ws" / "selftest").glob("*.step"))


def test_cli_print_config_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["print-config", "--profile", "design", "--allowed-dir", "C:\\Projets\\CAO"]) == 0
    block = json.loads(capsys.readouterr().out)["mcpServers"]["solidworks"]
    assert block["args"] == ["serve", "--profile", "design"] and block["env"] == {
        "SWMCP_ALLOWED_DIRS": "C:\\Projets\\CAO"
    }
    assert cli.main(["version"]) == 0
    assert "swmcp 0.1.0" in capsys.readouterr().out


def test_cli_install_client_backs_up(tmp_path: Path) -> None:
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(
        json.dumps({"mcpServers": {"autre": {"command": "x"}}, "theme": "dark"}), encoding="utf-8"
    )
    path, backup = cli.install_client("claude-desktop", cli.client_block("design", []), target)
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["theme"] == "dark" and set(config["mcpServers"]) == {"autre", "solidworks"}
    assert backup is not None and json.loads(backup.read_text(encoding="utf-8"))["mcpServers"] == {
        "autre": {"command": "x"}
    }
    fresh, no_backup = cli.install_client(
        "claude-code", cli.client_block("inspect", []), tmp_path / "new" / ".mcp.json"
    )
    assert no_backup is None and "solidworks" in json.loads(fresh.read_text(encoding="utf-8"))["mcpServers"]
    target.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        cli.install_client("claude-desktop", {}, target)


def test_cli_doctor_and_selftest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWMCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SWMCP_API_INDEX_PATH", str(tmp_path / "api.sqlite"))
    code = cli.main(["doctor", "--workspace", str(tmp_path / "ws"), "--backend", "fake"])
    out = capsys.readouterr().out
    assert (
        "Dossier de travail" in out and "contrôles" in out and code == (0 if sys.platform == "win32" else 1)
    )
    assert (
        cli.main(
            [
                "selftest",
                "--backend",
                "fake",
                "--workspace",
                str(tmp_path / "ws"),
                "--out",
                str(tmp_path / "r.json"),
            ]
        )
        == 0
    )
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["ok"] is True
    if sys.platform != "win32":
        assert cli.main(["index-api"]) == 1


# --- session MCP réelle en stdio --------------------------------------------------------------------
async def test_stdio_session_keeps_stdout_clean(tmp_path: Path) -> None:
    """Une session complète par le transport stdio : tout octet parasite sur stdout casserait le protocole."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("SWMCP_")},
        "SWMCP_BACKEND": "fake",
        "SWMCP_WORKSPACE": str(tmp_path / "ws"),
        "SWMCP_LOG_DIR": str(tmp_path / "logs"),
        "SWMCP_LOG_LEVEL": "DEBUG",
        "SWMCP_CONFIG": str(tmp_path / "absent.toml"),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "swmcp", "serve", "--profile", "design"], env=env
    )
    with open(tmp_path / "stderr.log", "w") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            assert init.serverInfo.name == "solidworks" and init.serverInfo.version == "0.1.0"
            assert init.instructions and "mm" in init.instructions
            tools = (await session.list_tools()).tools
            assert len(tools) == 53 and all(t.outputSchema for t in tools)
            assert len((await session.list_prompts()).prompts) == 4
            assert len((await session.list_resources()).resources) == 7
            result = await session.call_tool("sw_connect", {})
            assert result.structuredContent["ok"] and not result.isError  # type: ignore[index]
            await session.call_tool("doc_new", {"type": "part"})
            bad = await session.call_tool("feature_extrude", {"sketch": "x", "depth_mm": -1})
            assert bad.isError and bad.structuredContent["error"]["code"] == "INVALID_INPUT"  # type: ignore[index]
            shot = await session.call_tool("view_capture", {"views": ["iso"], "width_px": 200})
            assert [c.type for c in shot.content] == ["text", "image"]
    assert (tmp_path / "logs" / "audit.jsonl").is_file()
