"""`swmcp doctor` : diagnostic de l'environnement, avec une sortie lisible (spec §9, §11)."""

from __future__ import annotations

import os
import platform
import struct
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str | None = None


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".swmcp-", delete=True):
            pass
        return True, str(path)
    except OSError as exc:
        return False, f"{path} : {exc}"


def _check_com() -> list[Check]:
    out: list[Check] = []
    try:
        import pythoncom  # noqa: F401
        import win32com.client
    except ImportError:
        return [
            Check(
                "pywin32", "fail", "pywin32 n'est pas installé.", "Réinstaller swmcp (uv tool install swmcp)."
            )
        ]
    out.append(Check("pywin32", "ok", "importable"))
    try:
        import winreg as _winreg

        winreg: Any = _winreg

        # UNVERIFIED: le ProgID SldWorks.Application est enregistré sous HKEY_CLASSES_ROOT après un premier lancement.
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID") as key:
            clsid = winreg.QueryValue(key, None)
        out.append(Check("Enregistrement COM", "ok", f"SldWorks.Application → {clsid}"))
    except OSError:
        out.append(
            Check(
                "Enregistrement COM",
                "fail",
                "ProgID SldWorks.Application introuvable.",
                "Installer SolidWorks et le lancer au moins une fois.",
            )
        )
        return out
    try:
        # UNVERIFIED: EnsureDispatch génère les classes de liaison anticipée (gen_py) depuis sldworks.tlb.
        win32com.client.gencache.EnsureModule("{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, 0, 0)
        out.append(Check("Classes pywin32 générées", "ok", "bibliothèque de types SldWorks chargée"))
    except Exception as exc:
        out.append(
            Check(
                "Classes pywin32 générées",
                "warn",
                f"génération impossible : {exc}",
                "Lancer SolidWorks puis `swmcp doctor --connect` ; au besoin vider le cache gen_py.",
            )
        )
    return out


def _check_connection(settings: Settings) -> list[Check]:
    from .backend.com_backend import ComSwBackend

    out: list[Check] = []
    try:
        import pythoncom

        pythoncom.CoInitialize()
        backend = ComSwBackend()
        info = backend.connect(False, None)
        out.append(
            Check(
                "Connexion à SolidWorks",
                "ok",
                f"SolidWorks {info.version} (révision {info.revision}), pid {info.pid}",
            )
        )
        try:
            doc = backend.new_doc("part", None)
            backend.close_doc(doc.id, False)
            out.append(Check("Modèle de pièce par défaut", "ok", "doc_new(part) fonctionne"))
        except Exception as exc:
            out.append(
                Check(
                    "Modèle de pièce par défaut",
                    "fail",
                    str(exc)[:200],
                    "Configurer les modèles par défaut dans Options > Modèles par défaut de SolidWorks.",
                )
            )
        backend.shutdown()
    except Exception as exc:
        out.append(
            Check(
                "Connexion à SolidWorks",
                "fail",
                str(exc)[:200],
                "Ouvrir SolidWorks, fermer tout dialogue, puis relancer `swmcp doctor --connect`.",
            )
        )
    del settings
    return out


def run_checks(settings: Settings, connect: bool = False) -> list[Check]:
    checks: list[Check] = []
    is_win = sys.platform == "win32"
    checks.append(
        Check("Système", "ok", f"Windows {platform.release()}")
        if is_win
        else Check(
            "Système",
            "fail",
            f"{platform.system()} : SolidWorks exige Windows 10 ou 11.",
            "Sur ce poste, seul le backend simulé fonctionne (SWMCP_BACKEND=fake).",
        )
    )
    bits = struct.calcsize("P") * 8
    checks.append(
        Check(
            "Python",
            "ok" if sys.version_info >= (3, 11) and bits == 64 else "fail",
            f"{platform.python_version()} ({bits} bits)",
            None if bits == 64 else "Installer un Python 64 bits ≥ 3.11.",
        )
    )
    if is_win:
        checks += _check_com()
        if connect:
            checks += _check_connection(settings)
    for label, path in (("Dossier de travail", settings.workspace), ("Journaux", settings.log_dir)):
        ok, detail = _writable(path)
        checks.append(
            Check(
                label,
                "ok" if ok else "fail",
                detail,
                None if ok else "Vérifier les droits ou changer le dossier dans la configuration.",
            )
        )
    for d in settings.allowed_dirs:
        if d.is_dir():
            ok = os.access(d, os.R_OK | os.W_OK)
            checks.append(
                Check(
                    "Dossier autorisé",
                    "ok" if ok else "warn",
                    str(d),
                    None if ok else "Droits de lecture/écriture insuffisants.",
                )
            )
        else:
            checks.append(
                Check(
                    "Dossier autorisé",
                    "warn",
                    f"{d} n'existe pas",
                    "Créer le dossier ou corriger SWMCP_ALLOWED_DIRS.",
                )
            )
    checks.append(
        Check("Index de l'API", "ok", str(settings.api_index_path))
        if settings.api_index_path.is_file()
        else Check(
            "Index de l'API",
            "warn",
            "absent : api_search et api_describe renverront indexed=false",
            "Lancer `swmcp index-api`.",
        )
    )
    checks.append(
        Check("Profil", "ok", f"{settings.profile} (backend {settings.effective_backend}, transport stdio)")
    )
    return checks


def report(checks: list[Check], write: Callable[[str], object]) -> int:
    marks = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    for c in checks:
        write(f"{marks[c.status]} {c.name} : {c.detail}")
        if c.fix and c.status != "ok":
            write(f"       → {c.fix}")
    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    write("")
    write(f"{len(checks)} contrôles, {fails} échec(s), {warns} avertissement(s).")
    return 1 if fails else 0
