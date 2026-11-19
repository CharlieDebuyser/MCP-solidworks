from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from swmcp.backend.fake_backend import FakeSwBackend
from swmcp.config import Settings, load_settings
from swmcp.runtime import Runtime
from swmcp.server import SwFastMCP, create_server


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("SWMCP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SWMCP_CONFIG", str(tmp_path / "absent.toml"))


class Harness:
    def __init__(self, mcp: SwFastMCP, rt: Runtime, tmp: Path) -> None:
        self.mcp, self.rt, self.tmp = mcp, rt, tmp

    @property
    def fake(self) -> FakeSwBackend:
        backend = self.rt.worker.backend
        assert isinstance(backend, FakeSwBackend)
        return backend

    async def raw(self, tool: str, **args: Any) -> Any:
        return await self.mcp.call_tool(tool, args)

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = await self.raw(tool, **args)
        env: dict[str, Any] = result.structuredContent
        return env

    async def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        env = await self.call(tool, **args)
        assert env["ok"], env["error"]
        data: dict[str, Any] = env["data"]
        return data

    async def err(self, tool: str, code: str, **args: Any) -> dict[str, Any]:
        env = await self.call(tool, **args)
        assert not env["ok"], env["data"]
        assert env["error"]["code"] == code, env["error"]
        error: dict[str, Any] = env["error"]
        return error

    async def confirmed(self, tool: str, **args: Any) -> dict[str, Any]:
        error = await self.err(tool, "CONFIRMATION_REQUIRED", **args)
        return await self.ok(tool, **args, confirm_token=error["details"]["confirm_token"])

    async def box(self, x: float = 40, y: float = 30, z: float = 10, plane: str = "front") -> None:
        """Pavé x × y dans l'esquisse, extrudé de z."""
        await self.ok("sw_connect")
        await self.ok("doc_new", type="part")
        await self.ok("sketch_begin", plane=plane, name="Esq")
        await self.ok("sketch_add_rect", x1_mm=0, y1_mm=0, x2_mm=x, y2_mm=y)
        await self.ok("sketch_end")
        await self.ok("feature_extrude", sketch="Esq", depth_mm=z, name="Bloc")


def make_settings(tmp: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "profile": "full",
        "raw_api": True,
        "backend": "fake",
        "workspace": tmp / "ws",
        "allowed_dirs": [tmp],
        "log_dir": tmp / "logs",
        "api_index_path": tmp / "api.sqlite",
    }
    base.update(kw)
    return load_settings(**base)


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Any]:
    started: list[Runtime] = []

    def factory(**kw: Any) -> Harness:
        mcp, rt = create_server(make_settings(tmp_path, **kw))
        rt.start()
        started.append(rt)
        return Harness(mcp, rt, tmp_path)

    yield factory
    for rt in started:
        rt.stop()


@pytest.fixture
def h(make: Any) -> Harness:
    harness: Harness = make()
    return harness
