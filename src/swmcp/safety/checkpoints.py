"""Points de restauration par copie de fichier (spec §8).

Emplacement : `<dossier de travail>/.swmcp/checkpoints/<document>/<horodatage>-<libellé><ext>`.
La copie elle-même est faite par le backend (enregistrement en mode copie) ; ce module gère
les chemins, l'inventaire et la rétention.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from ..envelope import ErrorCode, SwError

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class Checkpoint(BaseModel):
    id: str
    doc: str
    label: str
    path: str
    created: str
    size_bytes: int


def _slug(text: str, default: str) -> str:
    s = _SAFE.sub("_", text).strip("._")[:60]
    return s or default


class CheckpointManager:
    def __init__(self, workspace: Path, max_per_doc: int = 20, max_total_mb: float = 500.0) -> None:
        self.root = workspace / ".swmcp" / "checkpoints"
        self.max_per_doc = max_per_doc
        self.max_total_bytes = int(max_total_mb * 1024 * 1024)

    def _doc_dir(self, doc_name: str) -> Path:
        return self.root / _slug(Path(doc_name).stem, "document")

    def plan(self, doc_name: str, label: str) -> Path:
        """Chemin de la prochaine copie (le dossier est créé)."""
        d = self._doc_dir(doc_name)
        d.mkdir(parents=True, exist_ok=True)
        ext = Path(doc_name).suffix.lower() or ".sldprt"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{_slug(label, 'point')}"
        candidate = d / f"{base}{ext}"
        n = 1
        while candidate.exists():
            n += 1
            candidate = d / f"{base}-{n}{ext}"
        return candidate

    def create(self, doc_name: str, label: str, save_copy: Callable[[str], None]) -> Checkpoint:
        target = self.plan(doc_name, label)
        save_copy(str(target))
        if not target.is_file():
            raise SwError(ErrorCode.INTERNAL, "La copie du point de restauration n'a pas été écrite.")
        self.prune(doc_name)
        return self._describe(target)

    def _describe(self, path: Path) -> Checkpoint:
        st = path.stat()
        stem = path.stem
        label = stem.split("-", 2)[2] if stem.count("-") >= 2 else stem
        return Checkpoint(
            id=f"{path.parent.name}/{path.name}",
            doc=path.parent.name,
            label=label,
            path=str(path),
            created=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
            size_bytes=st.st_size,
        )

    def _files(self, doc_name: str | None = None) -> list[Path]:
        if not self.root.is_dir():
            return []
        dirs = [self._doc_dir(doc_name)] if doc_name else [d for d in self.root.iterdir() if d.is_dir()]
        files = [f for d in dirs if d.is_dir() for f in d.iterdir() if f.is_file()]
        return sorted(files, key=lambda f: (f.stat().st_mtime, f.name))

    def list(self, doc_name: str | None = None) -> list[Checkpoint]:
        return [self._describe(f) for f in reversed(self._files(doc_name))]

    def resolve(self, checkpoint_id: str) -> Path:
        parts = checkpoint_id.replace("\\", "/").split("/")
        if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
            raise SwError(
                ErrorCode.INVALID_INPUT, f"Identifiant de point de restauration invalide : {checkpoint_id}"
            )
        path = self.root / parts[0] / parts[1]
        if not path.is_file():
            raise SwError(
                ErrorCode.INVALID_INPUT,
                f"Point de restauration introuvable : {checkpoint_id}",
                hint="Appeler checkpoint_list pour obtenir les identifiants valides.",
            )
        return path

    def prune(self, doc_name: str | None = None) -> int:
        """Applique la rétention ; renvoie le nombre de fichiers supprimés."""
        removed = 0
        if doc_name:
            files = self._files(doc_name)
            for f in files[: max(0, len(files) - self.max_per_doc)]:
                f.unlink(missing_ok=True)
                removed += 1
        all_files = self._files()
        total = sum(f.stat().st_size for f in all_files)
        while all_files and total > self.max_total_bytes and len(all_files) > 1:
            oldest = all_files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            removed += 1
        return removed
