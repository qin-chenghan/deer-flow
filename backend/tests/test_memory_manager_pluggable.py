"""Pluggable memory manager: the factory resolves the configured backend.

Covers the drop-in contract end-to-end:
- short name -> registered backend (deermem / noop);
- dotted path (``module.Attr`` and ``module:Attr``) -> the same class;
- unknown value -> DeerMem fallback.

Also pins the noop empty-memory behaviour and the ``hasattr`` capability
probing surface (reload_memory + fact CRUD) that the gateway/client rely on.

Each test resets the singleton + backend cache and sets the config, so they
are independent of order.
"""

from __future__ import annotations

import pytest

from deerflow.agents.memory import (
    MemoryManager,
    get_memory_manager,
    reset_memory_manager,
)
from deerflow.agents.memory.backends.deermem.deer_mem import DeerMem
from deerflow.agents.memory.backends.noop.noop_manager import NoopMemoryManager
from deerflow.config.memory_config import MemoryConfig, get_memory_config, set_memory_config


@pytest.fixture(autouse=True)
def _isolate_memory_manager():
    """Reset the singleton + restore config around every test."""
    orig = get_memory_config()
    reset_memory_manager()
    yield
    set_memory_config(orig)
    reset_memory_manager()


@pytest.mark.parametrize(
    "manager_class, expected",
    [
        ("deermem", DeerMem),
        ("noop", NoopMemoryManager),
        ("deerflow.agents.memory.backends.deermem.deer_mem.DeerMem", DeerMem),
        ("deerflow.agents.memory.backends.deermem.deer_mem:DeerMem", DeerMem),
        ("deerflow.agents.memory.backends.noop.noop_manager.NoopMemoryManager", NoopMemoryManager),
        ("deerflow.agents.memory.backends.noop.noop_manager:NoopMemoryManager", NoopMemoryManager),
    ],
)
def test_resolves_configured_backend(manager_class: str, expected: type[MemoryManager]) -> None:
    set_memory_config(MemoryConfig(manager_class=manager_class))
    manager = get_memory_manager()
    assert isinstance(manager, expected)
    # singleton: a second call returns the same instance
    assert get_memory_manager() is manager


def test_unknown_backend_falls_back_to_deermem() -> None:
    set_memory_config(MemoryConfig(manager_class="bogus-backend"))
    manager = get_memory_manager()
    assert isinstance(manager, DeerMem)


def test_noop_runs_with_empty_memory() -> None:
    set_memory_config(MemoryConfig(manager_class="noop"))
    manager = get_memory_manager()
    assert manager.get_context(user_id="u") == ""
    assert manager.search("anything") == []
    assert manager.get_memory(user_id="u") == {"facts": []}
    # writes are no-ops; memory stays empty
    manager.add("t", [], agent_name=None, user_id="u")
    manager.add_nowait("t", [], agent_name=None, user_id="u")
    assert manager.get_memory(user_id="u") == {"facts": []}


def test_internal_capabilities_are_hasattr_probeable() -> None:
    """reload_memory + fact CRUD + warm exist on DeerMem but not on noop (the ABC omits them)."""
    set_memory_config(MemoryConfig(manager_class="deermem"))
    deermem = get_memory_manager()
    for cap in ("warm", "reload_memory", "create_fact", "delete_fact", "update_fact"):
        assert hasattr(deermem, cap), cap

    reset_memory_manager()
    set_memory_config(MemoryConfig(manager_class="noop"))
    noop = get_memory_manager()
    for cap in ("warm", "reload_memory", "create_fact", "delete_fact", "update_fact"):
        assert not hasattr(noop, cap), cap


def test_deermem_stubs_raise_not_implemented() -> None:
    set_memory_config(MemoryConfig(manager_class="deermem"))
    deermem = get_memory_manager()
    with pytest.raises(NotImplementedError):
        deermem.search("q", user_id="u")
    with pytest.raises(NotImplementedError):
        deermem.delete_memory(user_id="u")
    with pytest.raises(NotImplementedError):
        deermem.export_memory(user_id="u")
