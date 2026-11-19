from __future__ import annotations

import pytest

from ..conftest import Harness
from .level1 import TASKS, Task, grade


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_level1_scripted(h: Harness, task: Task) -> None:
    """Porte M2 : 100 % de réussite du niveau 1 par séquence scriptée (sur le backend simulé)."""
    await h.ok("sw_connect")
    await task.script(h.ok)
    assert await grade(h.ok, task) == []


def test_ten_tasks_with_statements() -> None:
    assert len(TASKS) == 10 and len({t.id for t in TASKS}) == 10 and all(len(t.statement) > 20 for t in TASKS)
