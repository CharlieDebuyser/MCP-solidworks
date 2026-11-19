"""Journal d'audit et journalisation structurée (spec §8, §9).

Tout part vers un fichier et vers stderr, jamais vers stdout : en stdio, stdout est le canal du
protocole MCP.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
_MAX_STR = 200
_MAX_ITEMS = 20


def truncate(value: Any, depth: int = 0) -> Any:
    """Tronque les paramètres pour le journal ; aucun contenu binaire."""
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} octets>"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, int | float | bool) or value is None:
        return value
    if depth >= 4:
        return "…"
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_ITEMS]
        return {str(k): ("***" if "token" in str(k).lower() else truncate(v, depth + 1)) for k, v in items}
    if isinstance(value, list | tuple | set | frozenset):
        seq = list(value)
        out = [truncate(v, depth + 1) for v in seq[:_MAX_ITEMS]]
        if len(seq) > _MAX_ITEMS:
            out.append(f"… (+{len(seq) - _MAX_ITEMS})")
        return out
    return truncate(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "swmcp", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path | None, level: str = "INFO") -> None:
    """Configure le logger `swmcp` : stderr + fichier tournant. Idempotent."""
    root = logging.getLogger("swmcp")
    root.setLevel(level)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(JsonFormatter())
    root.addHandler(stderr)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_dir / "swmcp.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
            )
            fh.setFormatter(JsonFormatter())
            root.addHandler(fh)
        except OSError as exc:  # le journal ne doit jamais empêcher le serveur de démarrer
            root.warning("journal fichier indisponible: %s", exc)


class AuditLog:
    """Une ligne JSON par appel d'outil."""

    def __init__(self, log_dir: Path | None, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._logger = logging.getLogger(f"swmcp.audit.{self.session_id}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self.path: Path | None = None
        if log_dir is not None:
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                self.path = log_dir / "audit.jsonl"
                handler = RotatingFileHandler(
                    self.path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._logger.addHandler(handler)
            except OSError:
                self.path = None

    def record(
        self,
        *,
        tool: str,
        params: dict[str, Any],
        code: str,
        elapsed_ms: int,
        doc: str | None,
        request_id: str | None = None,
    ) -> None:
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session": self.session_id,
            "request": request_id,
            "tool": tool,
            "params": truncate(params),
            "code": code,
            "elapsed_ms": elapsed_ms,
            "doc": doc,
        }
        self._logger.info(json.dumps(line, ensure_ascii=False, default=str))

    def close(self) -> None:
        for h in list(self._logger.handlers):
            self._logger.removeHandler(h)
            h.close()
