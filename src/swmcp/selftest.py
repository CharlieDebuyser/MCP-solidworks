"""`swmcp selftest` : séquence automatique jouée à travers la couche MCP (spec §14).

Créer une pièce, extruder, percer, mesurer, capturer, exporter, fermer ; le rapport JSON liste
réussites et échecs pour corriger vite au premier essai sur un vrai SolidWorks.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any

from . import TOOL_SCHEMA_VERSION, __version__
from .config import Settings
from .server import create_server

L_PROFILE = [[0, 0], [60, 0], [60, 3], [3, 3], [3, 40], [0, 40]]
L_VOLUME = (60 * 3 + 3 * 37) * 30.0
HOLES_VOLUME = 2 * math.pi * 2.5**2 * 3


async def run_selftest(settings: Settings) -> dict[str, Any]:
    mcp, rt = create_server(settings)
    rt.start()
    steps: list[dict[str, Any]] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = settings.workspace / "selftest"
    ctx: dict[str, Any] = {}

    async def step(name: str, tool: str, args: dict[str, Any], check: Any = None) -> dict[str, Any] | None:
        t0 = time.perf_counter()
        entry: dict[str, Any] = {"step": name, "tool": tool}
        try:
            result = await mcp.call_tool(tool, args)
            env = result.structuredContent  # type: ignore[union-attr]
            entry["ok"] = bool(env["ok"])
            if not env["ok"]:
                entry["error"] = env["error"]
            elif check is not None:
                problem = check(env["data"])
                if problem:
                    entry["ok"], entry["error"] = False, {"code": "CHECK_FAILED", "message": problem}
            entry["warnings"] = env.get("warnings", [])
            data = env["data"] if env["ok"] else None
        except Exception as exc:  # le rapport doit toujours sortir
            entry["ok"], entry["error"] = (
                False,
                {"code": "EXCEPTION", "message": f"{type(exc).__name__}: {exc}"},
            )
            data = None
        entry["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        steps.append(entry)
        return data

    def near(expected: float, pct: float, key: str = "volume_mm3") -> Any:
        def check(d: dict[str, Any]) -> str | None:
            got = d[key]
            if abs(got - expected) / expected * 100 > pct:
                return f"{key} = {got:.3f}, attendu {expected:.3f} ± {pct} %"
            return None

        return check

    try:
        conn = await step("connexion", "sw_connect", {})
        await step("statut", "sw_status", {})
        await step("nouvelle pièce", "doc_new", {"type": "part"})
        begun = await step(
            "esquisse sur le plan de face", "sketch_begin", {"plane": "front", "name": "Profil"}
        )
        ctx["frame"] = begun["frame"] if begun else None
        poly = await step("profil en L", "sketch_add_polyline", {"points_mm": L_PROFILE, "closed": True})
        if poly:
            lines = [e["handle"] for e in poly["entities"] if e["type"] == "line"]
            points = [e["handle"] for e in poly["entities"] if e["type"] == "point"]
            await step(
                "relations horizontales",
                "sketch_add_relation",
                {"type": "horizontal", "entities": lines[0::2]},
            )
            await step(
                "relations verticales", "sketch_add_relation", {"type": "vertical", "entities": lines[1::2]}
            )
            await step(
                "origine", "sketch_add_relation", {"type": "coincident", "entities": [points[0], "origin"]}
            )
            await step(
                "cote longueur",
                "sketch_add_dimension",
                {"type": "length", "entities": [lines[0]], "value": 60, "name": "Longueur"},
            )
            await step(
                "cote épaisseur",
                "sketch_add_dimension",
                {"type": "length", "entities": [lines[1]], "value": 3, "name": "Epaisseur"},
            )
        await step("fin d'esquisse", "sketch_end", {}, lambda d: None if d["closed"] else "contour non fermé")
        await step(
            "extrusion",
            "feature_extrude",
            {"sketch": "Profil", "depth_mm": 30, "name": "Base", "verify": "full"},
            lambda d: near(L_VOLUME, 1.0)(d["verify"]),
        )
        faces = await step(
            "face du dessus de l'aile",
            "find_faces",
            {"planar": True, "normal": [0, 1, 0], "near_point_mm": [50, 3, 15], "limit": 1},
            lambda d: None if d["items"] else "aucune face trouvée",
        )
        if faces and faces["items"]:
            await step(
                "perçages",
                "feature_hole",
                {
                    "face": faces["items"][0]["handle"],
                    "points_mm": [[50, 3, 10], [50, 3, 20]],
                    "diameter_mm": 5,
                    "through": True,
                    "name": "Percages",
                },
                lambda d: near(L_VOLUME - HOLES_VOLUME, 1.0)(d["verify"]),
            )
        await step("masse", "inspect_mass", {}, near(L_VOLUME - HOLES_VOLUME, 1.0))
        await step(
            "contrôle",
            "check_design",
            {
                "rules": [
                    {"rule": "no_rebuild_errors"},
                    {"rule": "single_body"},
                    {"rule": "bbox_within", "size_mm": [60, 40, 30]},
                    {"rule": "volume_within", "volume_mm3": L_VOLUME - HOLES_VOLUME},
                ]
            },
            lambda d: (
                None
                if d["passed"]
                else "règles en échec : " + ", ".join(r["rule"] for r in d["results"] if not r["passed"])
            ),
        )
        await step(
            "cote d'extrusion",
            "dimension_set",
            {"full_name": "D1@Base", "value": 40},
            lambda d: (
                None if d["volume_after_mm3"] > d["volume_before_mm3"] else "le volume n'a pas augmenté"
            ),
        )
        await step("arbre", "inspect_tree", {})
        await step("capture", "view_capture", {"views": ["iso"], "width_px": 400})
        await step("enregistrement", "doc_save", {"path": str(out_dir / f"selftest-{stamp}.sldprt")})
        await step(
            "export STEP", "export_file", {"format": "step", "path": str(out_dir / f"selftest-{stamp}.step")}
        )
        await step("fermeture", "doc_close", {"save": False})
    finally:
        rt.stop()
    failed = [s for s in steps if not s["ok"]]
    return {
        "server_version": __version__,
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "backend": settings.effective_backend,
        "solidworks": conn,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passed": len(steps) - len(failed),
        "failed": len(failed),
        "ok": not failed,
        "steps": steps,
    }


def selftest(settings: Settings, out: Path | None = None) -> dict[str, Any]:
    import json

    report = asyncio.run(run_selftest(settings))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
