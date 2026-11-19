"""Protocole de confirmation en deux temps (spec §8).

Un jeton aléatoire, lié à l'outil et à ses paramètres, valable 120 s et à usage unique.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..envelope import ErrorCode, SwError


def fingerprint(tool: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if k != "confirm_token"}
    blob = json.dumps([tool, clean], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class _Pending:
    token: str
    fingerprint: str
    expires: float


class ConfirmStore:
    def __init__(self, ttl_s: float = 120.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._pending: dict[str, _Pending] = {}

    def _purge(self) -> None:
        now = self._clock()
        for fp in [fp for fp, p in self._pending.items() if p.expires <= now]:
            del self._pending[fp]

    def issue(self, tool: str, params: dict[str, Any]) -> str:
        self._purge()
        fp = fingerprint(tool, params)
        token = secrets.token_urlsafe(18)
        self._pending[fp] = _Pending(token, fp, self._clock() + self.ttl_s)
        return token

    def consume(self, tool: str, params: dict[str, Any], token: str) -> bool:
        """Vrai si le jeton est valide pour cet outil et ces paramètres ; il est alors détruit."""
        self._purge()
        fp = fingerprint(tool, params)
        pending = self._pending.get(fp)
        if pending is None:
            return False
        if not hmac.compare_digest(pending.token.encode(), token.encode()):
            return False
        del self._pending[fp]
        return True

    def require(self, tool: str, params: dict[str, Any], token: str | None, effect: str) -> None:
        """Lève CONFIRMATION_REQUIRED tant qu'un jeton valide n'est pas présenté."""
        if token and self.consume(tool, params, token):
            return
        new_token = self.issue(tool, params)
        message = f"Confirmation requise : {effect}"
        if token:
            message = f"Jeton invalide, expiré ou paramètres modifiés. {message}"
        raise SwError(
            ErrorCode.CONFIRMATION_REQUIRED,
            message,
            details={"effect": effect, "confirm_token": new_token, "expires_in_s": int(self.ttl_s)},
        )
