"""Liste blanche de chemins (spec §8).

Le LLM est une source non fiable : tout chemin est normalisé (liens symboliques résolus) puis
comparé aux dossiers autorisés. Les `..`, chemins UNC et noms de périphériques sont rejetés avant
toute résolution, sur la chaîne brute, pour que le contrôle soit identique sur toutes les plateformes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from ..envelope import ErrorCode, SwError

_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_SPLIT = re.compile(r"[\\/]+")
_FORBIDDEN_CHARS = re.compile(r'[\x00-\x1f<>"|?*]')

EXT_MODELS = frozenset({".sldprt", ".sldasm", ".slddrw"})
EXT_TEMPLATES = frozenset({".prtdot", ".asmdot", ".drwdot"})
EXT_EXPORT: dict[str, frozenset[str]] = {
    "step": frozenset({".step", ".stp"}),
    "stl": frozenset({".stl"}),
    "iges": frozenset({".igs", ".iges"}),
    "pdf": frozenset({".pdf"}),
    "dxf": frozenset({".dxf"}),
    "png": frozenset({".png"}),
}


def denied(path: str, reason: str) -> SwError:
    return SwError(ErrorCode.PATH_DENIED, f"Chemin refusé ({reason}) : {path[:200]}")


def lexical_check(raw: str) -> None:
    """Contrôles sur la chaîne brute, indépendants de la plateforme."""
    if not raw or not raw.strip():
        raise denied(raw, "chemin vide")
    if _FORBIDDEN_CHARS.search(raw):
        raise denied(raw, "caractère interdit")
    if raw.startswith(("\\\\", "//")):
        raise denied(raw, "chemin UNC ou de périphérique")
    parts = [p for p in _SPLIT.split(raw) if p]
    for i, part in enumerate(parts):
        if part == "..":
            raise denied(raw, "composant '..'")
        if ":" in part and not (i == 0 and re.fullmatch(r"[A-Za-z]:", part)):
            raise denied(raw, "flux de données ou lecteur invalide")
        stem = part.split(".")[0].strip().upper()
        if stem in _DEVICE_NAMES:
            raise denied(raw, "nom de périphérique")
        if part != "." and part.endswith((" ", ".")):
            raise denied(raw, "composant terminé par un point ou une espace")


class PathGuard:
    def __init__(self, allowed_dirs: Iterable[Path], workspace: Path) -> None:
        self.workspace = workspace
        self.allowed = [self._real(d) for d in allowed_dirs]

    @staticmethod
    def _real(p: Path) -> Path:
        return Path(os.path.realpath(p))

    def is_allowed(self, real: Path) -> bool:
        return any(real == base or base in real.parents for base in self.allowed)

    def check(
        self,
        raw: str,
        *,
        extensions: frozenset[str] | None = None,
        must_exist: bool = False,
        write: bool = False,
    ) -> Path:
        """Valide `raw` et renvoie le chemin réel absolu. Les chemins relatifs partent du dossier de travail."""
        lexical_check(raw)
        p = Path(os.path.expandvars(raw)).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        real = self._real(p)
        if not self.is_allowed(real):
            raise denied(raw, "hors des dossiers autorisés")
        if extensions is not None and real.suffix.lower() not in extensions:
            raise SwError(
                ErrorCode.INVALID_INPUT,
                f"Extension '{real.suffix}' non autorisée pour cet outil.",
                hint=f"Extensions acceptées : {', '.join(sorted(extensions))}.",
            )
        if must_exist and not real.is_file():
            raise SwError(
                ErrorCode.INVALID_INPUT,
                f"Fichier introuvable : {raw[:200]}",
                hint="Vérifier le chemin ; il doit désigner un fichier existant dans un dossier autorisé.",
            )
        if write:
            parent = real.parent
            if not parent.is_dir():
                if not self.is_allowed(self._real(parent)):
                    raise denied(raw, "dossier parent hors des dossiers autorisés")
                parent.mkdir(parents=True, exist_ok=True)
            if real.is_dir():
                raise SwError(ErrorCode.INVALID_INPUT, f"Le chemin désigne un dossier : {raw[:200]}")
        return real

    def check_dir(self, raw: str) -> Path:
        lexical_check(raw)
        p = Path(os.path.expandvars(raw)).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        real = self._real(p)
        if not self.is_allowed(real):
            raise denied(raw, "hors des dossiers autorisés")
        if not real.is_dir():
            raise SwError(ErrorCode.INVALID_INPUT, f"Dossier introuvable : {raw[:200]}")
        return real
