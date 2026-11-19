"""Ligne de commande `swmcp` (spec §11).

`serve` n'écrit jamais sur stdout (canal du protocole MCP) ; les autres commandes y affichent
leur résultat pour l'utilisateur.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import TOOL_SCHEMA_VERSION, __version__
from .config import Settings, load_settings

CLIENTS = ("claude-desktop", "claude-code")


def _settings(args: argparse.Namespace) -> Settings:
    dirs = getattr(args, "allowed_dir", None)
    return load_settings(
        profile=getattr(args, "profile", None),
        workspace=getattr(args, "workspace", None),
        allowed_dirs=dirs or None,
        backend=getattr(args, "backend", None),
        log_level=getattr(args, "log_level", None),
        timeout_s=getattr(args, "timeout", None),
    )


def client_block(profile: str, allowed_dirs: list[str], command: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "command": command or shutil.which("swmcp") or "swmcp",
        "args": ["serve", "--profile", profile],
    }
    if allowed_dirs:
        block["env"] = {"SWMCP_ALLOWED_DIRS": ";".join(allowed_dirs)}
    return block


def client_config_path(client: str) -> Path:
    import os

    if client == "claude-code":
        return Path.cwd() / ".mcp.json"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def install_client(client: str, block: dict[str, Any], path: Path | None = None) -> tuple[Path, Path | None]:
    """Ajoute le serveur à la configuration du client, après sauvegarde du fichier existant."""
    target = path or client_config_path(client)
    backup: Path | None = None
    config: dict[str, Any] = {}
    if target.is_file():
        text = target.read_text(encoding="utf-8")
        config = json.loads(text) if text.strip() else {}
        if not isinstance(config, dict):
            raise ValueError(f"{target} ne contient pas un objet JSON.")
        backup = target.with_name(f"{target.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(target, backup)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"`mcpServers` n'est pas un objet dans {target}.")
    servers["solidworks"] = block
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target, backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swmcp", description="Serveur MCP local pour SolidWorks.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", choices=("inspect", "design", "full"))
        p.add_argument("--workspace", type=Path)
        p.add_argument("--allowed-dir", action="append", type=Path, help="Dossier autorisé (répétable)")
        p.add_argument("--backend", choices=("auto", "com", "fake"), help="fake : simulation sans SolidWorks")
        p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
        p.add_argument("--timeout", type=float, help="Délai par appel, en secondes")

    common(sub.add_parser("serve", help="Lance le serveur MCP (stdio)"))
    doctor = sub.add_parser("doctor", help="Diagnostic de l'environnement")
    common(doctor)
    doctor.add_argument("--connect", action="store_true", help="Teste aussi la connexion à SolidWorks ouvert")
    index = sub.add_parser("index-api", help="Construit l'index de l'API depuis les bibliothèques de types")
    index.add_argument(
        "--tlb", action="append", type=Path, help="Chemin d'une .tlb (répétable) ; défaut : détection"
    )
    for name in ("install-client", "print-config"):
        p = sub.add_parser(name, help="Configuration d'un client MCP")
        if name == "install-client":
            p.add_argument("client", choices=CLIENTS)
        else:
            p.add_argument("--client", choices=CLIENTS, default="claude-desktop")
        common(p)
    sub.add_parser("version", help="Versions du serveur et de SolidWorks détecté")
    st = sub.add_parser("selftest", help="Séquence automatique de bout en bout, rapport JSON")
    common(st)
    st.add_argument("--out", type=Path, help="Fichier du rapport JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from .server import serve

        serve(_settings(args))
        return 0
    if args.command == "doctor":
        from .doctor import report, run_checks

        return report(run_checks(_settings(args), args.connect), print)
    if args.command == "index-api":
        from .apiindex import build_index

        settings = load_settings()
        try:
            stats = build_index(settings.api_index_path, args.tlb)
        except RuntimeError as exc:
            print(f"Échec : {exc}", file=sys.stderr)
            return 1
        print(f"Index écrit dans {settings.api_index_path} : {json.dumps(stats)}")
        return 0
    if args.command in ("install-client", "print-config"):
        settings = _settings(args)
        explicit = [str(p) for p in (args.allowed_dir or [])]
        block = client_block(settings.profile, explicit)
        if args.command == "print-config":
            print(json.dumps({"mcpServers": {"solidworks": block}}, ensure_ascii=False, indent=2))
            return 0
        try:
            target, backup = install_client(args.client, block)
        except (OSError, ValueError) as exc:
            print(f"Échec : {exc}", file=sys.stderr)
            return 1
        print(f"Serveur 'solidworks' ajouté à {target}" + (f" (sauvegarde : {backup})" if backup else ""))
        print("Redémarrer le client pour charger la configuration.")
        return 0
    if args.command == "version":
        print(f"swmcp {__version__} (schéma d'outils {TOOL_SCHEMA_VERSION})")
        if sys.platform == "win32":
            try:
                import pythoncom

                from .backend.com_backend import ComSwBackend

                pythoncom.CoInitialize()
                info = ComSwBackend().connect(False, None)
                print(f"SolidWorks {info.version} (révision {info.revision})")
            except Exception as exc:
                print(f"SolidWorks : non détecté ({str(exc)[:120]})")
        else:
            print("SolidWorks : non disponible sur cette plateforme (backend simulé seulement)")
        return 0
    if args.command == "selftest":
        from .safety.audit import setup_logging
        from .selftest import selftest

        settings = _settings(args)
        setup_logging(settings.log_dir, "WARNING")
        rep = selftest(settings, args.out)
        for s in rep["steps"]:
            line = f"[{'OK' if s['ok'] else 'ÉCHEC'}] {s['step']} ({s['tool']}, {s['elapsed_ms']} ms)"
            if not s["ok"]:
                line += f" — {s['error'].get('code')}: {s['error'].get('message')}"
            print(line)
        print(f"\n{rep['passed']} réussite(s), {rep['failed']} échec(s) — backend {rep['backend']}")
        if args.out:
            print(f"Rapport : {args.out}")
        return 0 if rep["ok"] else 1
    return 2
