"""Modèle de configuration unique (spec §11).

Priorité : options de ligne de commande > variables d'environnement `SWMCP_*` > fichier TOML.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

Profile = Literal["inspect", "design", "full"]
PROFILE_RANK: dict[str, int] = {"inspect": 0, "design": 1, "full": 2}


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / ".config"))


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "state"))


def config_file() -> Path:
    return _appdata() / "swmcp" / "config.toml"


def data_dir() -> Path:
    return _localappdata() / "swmcp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SWMCP_", extra="ignore", populate_by_name=True)

    profile: Profile = "design"
    workspace: Path = Field(default_factory=lambda: Path.home() / "Documents" / "swmcp-workspace")
    allowed_dirs: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    launch_sw: bool = False
    timeout_s: float = Field(default=60.0, gt=0)
    long_timeout_s: float = Field(default=300.0, gt=0)
    auto_reconnect: bool = True
    raw_api: bool = False
    raw_api_allow: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "app.RevisionNumber",
            "app.GetProcessID",
            "app.GetCurrentLanguage",
            "doc.GetTitle",
            "doc.GetPathName",
            "doc.GetType",
            "doc.GetFeatureCount",
        ],
        description="Liste blanche `objet.Méthode` de api_call_raw.",
    )
    transport: Literal["stdio"] = "stdio"  # HTTP hors v1 (spec §14)
    backend: Literal["auto", "com", "fake"] = "auto"
    log_dir: Path = Field(default_factory=lambda: data_dir() / "logs")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_calls_per_minute: int = Field(default=240, gt=0)
    max_queue: int = Field(default=10, gt=0)
    max_export_mb: float = Field(default=500.0, gt=0)
    checkpoint_max_per_doc: int = Field(default=20, gt=0)
    checkpoint_max_total_mb: float = Field(default=500.0, gt=0)
    confirm_ttl_s: float = Field(default=120.0, gt=0)
    api_index_path: Path = Field(default_factory=lambda: data_dir() / "apiindex.sqlite")

    @field_validator("allowed_dirs", mode="before")
    @classmethod
    def _split_dirs(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [p.strip() for p in v.split(";") if p.strip()]
        return v

    @field_validator("raw_api_allow", mode="before")
    @classmethod
    def _split_allow(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [p.strip() for p in v.replace(",", ";").split(";") if p.strip()]
        return v

    @model_validator(mode="after")
    def _defaults(self) -> Settings:
        self.workspace = Path(os.path.expandvars(str(self.workspace))).expanduser()
        self.allowed_dirs = [Path(os.path.expandvars(str(p))).expanduser() for p in self.allowed_dirs]
        if not self.allowed_dirs:
            # Sans configuration, seul le dossier de travail est autorisé (spec §8).
            self.allowed_dirs = [self.workspace]
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        path = Path(os.environ.get("SWMCP_CONFIG") or config_file())
        if path.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=path))
        return tuple(sources)

    @property
    def effective_backend(self) -> Literal["com", "fake"]:
        if self.backend == "auto":
            return "com" if sys.platform == "win32" else "fake"
        return self.backend

    @property
    def read_only(self) -> bool:
        return self.profile == "inspect"

    def allows(self, profile: str) -> bool:
        return PROFILE_RANK[self.profile] >= PROFILE_RANK[profile]


def load_settings(**overrides: Any) -> Settings:
    """Charge la configuration ; `overrides` porte les options de ligne de commande non nulles."""
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
