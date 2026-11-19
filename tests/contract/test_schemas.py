"""Tests de contrat : instantanés des schémas JSON de chaque outil et de l'enveloppe (spec §12).

Toute différence signale une modification du contrat. Si elle est voulue :
`SWMCP_UPDATE_SNAPSHOTS=1 pytest tests/contract`, puis incrémenter `TOOL_SCHEMA_VERSION` et la
version majeure si la modification est incompatible (renommage, suppression de champ).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from swmcp import TOOL_SCHEMA_VERSION
from swmcp.envelope import Envelope, ErrorCode

from ..conftest import Harness

SNAPSHOT = Path(__file__).parent / "snapshots" / "tools.json"
UPDATE = os.environ.get("SWMCP_UPDATE_SNAPSHOTS") == "1"


async def current(h: Harness) -> dict[str, Any]:
    tools = sorted(await h.mcp.list_tools(), key=lambda t: t.name)
    return {
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "error_codes": [c.value for c in ErrorCode],
        "envelope": Envelope[Any].model_json_schema(),
        "tools": {
            t.name: {
                "input": t.inputSchema,
                "output": t.outputSchema,
                "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None,
            }
            for t in tools
        },
    }


async def test_tool_schemas_match_snapshot(h: Harness) -> None:
    now = json.loads(json.dumps(await current(h), ensure_ascii=False, sort_keys=True))
    if UPDATE or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(now, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert saved["tool_schema_version"] == now["tool_schema_version"]
    assert saved["error_codes"] == now["error_codes"]
    assert saved["envelope"] == now["envelope"]
    assert sorted(saved["tools"]) == sorted(now["tools"]), "outil ajouté ou retiré"
    changed = [name for name in now["tools"] if saved["tools"][name] != now["tools"][name]]
    assert not changed, f"schéma modifié pour : {changed} (voir l'en-tête de ce fichier)"


async def test_catalog_matches_spec(h: Harness) -> None:
    expected = {
        "sw_connect",
        "sw_status",
        "doc_list_open",
        "doc_new",
        "doc_open",
        "doc_activate",
        "doc_save",
        "doc_close",
        "doc_info",
        "prop_get",
        "prop_set",
        "checkpoint_create",
        "checkpoint_list",
        "checkpoint_restore",
        "sketch_begin",
        "sketch_end",
        "sketch_add_line",
        "sketch_add_rect",
        "sketch_add_circle",
        "sketch_add_arc",
        "sketch_add_polyline",
        "sketch_add_slot",
        "sketch_add_point",
        "sketch_add_relation",
        "sketch_add_dimension",
        "sketch_inspect",
        "feature_extrude",
        "feature_cut_extrude",
        "feature_revolve",
        "feature_fillet",
        "feature_chamfer",
        "feature_hole",
        "feature_shell",
        "feature_pattern_linear",
        "feature_pattern_circular",
        "feature_mirror",
        "dimension_set",
        "variable_set",
        "feature_rename",
        "feature_suppress",
        "feature_delete",
        "inspect_tree",
        "inspect_rebuild",
        "inspect_mass",
        "inspect_dimensions",
        "find_faces",
        "find_edges",
        "inspect_measure",
        "inspect_interference",
        "check_design",
        "view_capture",
        "export_file",
        "api_search",
        "api_describe",
        "knowledge_get",
        "api_call_raw",
    }
    assert {t.name for t in await h.mcp.list_tools()} == expected  # aucun outil hors catalogue (consigne 4)
