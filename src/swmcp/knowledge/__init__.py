"""Fiches de connaissance CAO servies au LLM (spec §7).

Les fiches sont des fichiers Markdown de ce dossier, avec un en-tête `title`, `when_to_use`,
`version`, `date`. `instructions.md` alimente le champ `instructions` du serveur.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

_EXCLUDED = {"instructions", "CHANGELOG"}
URI_PREFIX = "swmcp://knowledge/"


@dataclass(frozen=True)
class Sheet:
    topic: str
    title: str
    when_to_use: str
    version: str | None
    date: str | None
    body: str

    @property
    def uri(self) -> str:
        return URI_PREFIX + self.topic


def _parse(topic: str, text: str) -> Sheet:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        head, _, rest = text[3:].partition("\n---")
        body = rest.lstrip("\n")
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
    return Sheet(
        topic=topic,
        title=meta.get("title", topic),
        when_to_use=meta.get("when_to_use", ""),
        version=meta.get("version"),
        date=meta.get("date"),
        body=body,
    )


@lru_cache(maxsize=1)
def sheets() -> dict[str, Sheet]:
    out: dict[str, Sheet] = {}
    for entry in sorted(resources.files(__package__).iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md") and entry.name[:-3] not in _EXCLUDED:
            topic = entry.name[:-3]
            out[topic] = _parse(topic, entry.read_text(encoding="utf-8"))
    return out


@lru_cache(maxsize=1)
def instructions() -> str:
    return resources.files(__package__).joinpath("instructions.md").read_text(encoding="utf-8")
