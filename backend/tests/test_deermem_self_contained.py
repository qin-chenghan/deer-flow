"""Phase-2 (self-contained DeerMem) tests.

Covers: DI construction (owns storage/updater/queue/llm), zero-config defaults,
``trace_id`` threading to the optional ``tracing_callback``, langfuse being
optional, ``hide_from_ui`` default-skip + hook-keep, empty ``storage_class``
(portable default), and portability -- ``backends/deermem/`` has exactly one
``from deerflow`` line (the ABC contract) and can be vendored into another agent
by copying the folder and repointing that one line.

Storage is isolated via ``$DEERMEM_DATA_DIR`` -> ``tmp_path``; the LLM is a fake
injected onto the updater so no network is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.memory.backends.deermem.deer_mem import DeerMem
from deerflow.agents.memory.backends.deermem.deermem.core.message_processing import (
    filter_messages_for_memory,
)
from deerflow.agents.memory.backends.deermem.deermem.core.storage import FileMemoryStorage


@pytest.fixture
def deermem_data_dir(tmp_path, monkeypatch):
    """Isolate DeerMem storage under tmp_path via $DEERMEM_DATA_DIR."""
    d = tmp_path / "deermem_data"
    d.mkdir()
    monkeypatch.setenv("DEERMEM_DATA_DIR", str(d))
    yield d


class _FakeLLM:
    """Returns a fixed memory-update JSON so no real LLM/network is needed."""

    def __init__(self, payload: str | None = None) -> None:
        self._payload = payload or '{"user":{},"history":{},"newFacts":[],"factsToRemove":[]}'

    def invoke(self, prompt, config=None):
        return type("R", (), {"content": self._payload})()


def _deermem_with_fake_llm(backend_config=None, payload=None) -> DeerMem:
    dm = DeerMem(backend_config=backend_config)
    fake = _FakeLLM(payload)
    dm._llm = fake
    dm._updater._llm = fake
    return dm


def test_di_construction_owns_dependencies():
    dm = DeerMem(backend_config={"max_facts": 50, "storage_path": "/tmp/x"})
    assert dm._config.max_facts == 50
    assert dm._storage is not None and dm._updater is not None and dm._queue is not None
    # dependencies are wired (DI), not globals:
    assert dm._updater._storage is dm._storage
    assert dm._queue._updater is dm._updater


def test_zero_config_defaults_run_non_llm_ops(deermem_data_dir):
    dm = DeerMem(backend_config=None)  # zero config
    assert dm._llm is None  # no model -> no LLM
    dm.import_memory(
        {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": [{"id": "f", "content": "x", "category": "c", "confidence": 0.5, "createdAt": "", "source": "m"}]},
        user_id="u",
    )
    assert "x" in dm.get_context(user_id="u")
    assert dm.get_memory(user_id="u")["facts"][0]["content"] == "x"


def test_trace_id_threads_through_to_tracing_callback(deermem_data_dir):
    calls = []

    def tracer(cfg, *, thread_id, user_id, trace_id, model_name):
        calls.append((thread_id, trace_id, model_name))

    dm = _deermem_with_fake_llm({"tracing_callback": tracer, "model": {"provider": "openai", "model": "gpt-x", "api_key": "k", "base_url": "u"}})
    dm.add(
        thread_id="t1",
        messages=[HumanMessage(content="hi"), AIMessage(content="hello")],
        agent_name=None,
        user_id="u1",
        trace_id="trace-42",
    )
    dm._queue.flush()
    assert calls and calls[0] == ("t1", "trace-42", "gpt-x")


def test_tracing_callback_optional_no_langfuse(deermem_data_dir):
    dm = _deermem_with_fake_llm({"model": {"provider": "openai", "model": "gpt-x", "api_key": "k", "base_url": "u"}})
    assert dm._config.tracing_callback is None  # langfuse not hard-required
    dm.add(
        thread_id="t2",
        messages=[HumanMessage(content="hi"), AIMessage(content="hello")],
        agent_name=None,
        user_id="u2",
        trace_id="t-99",
    )
    dm._queue.flush()  # no callback, no error, update completes


def test_hide_from_ui_default_skip_hook_keeps():
    hidden = HumanMessage(content="secret", additional_kwargs={"hide_from_ui": True})
    normal = HumanMessage(content="hi")
    ai = AIMessage(content="hello")
    # default (no hook) -> hide_from_ui skipped
    assert hidden not in filter_messages_for_memory([hidden, normal, ai])
    # hook returns True -> hidden kept
    assert hidden in filter_messages_for_memory([hidden, normal, ai], should_keep_hidden_message=lambda ak: True)


def test_storage_class_empty_uses_filememorystorage():
    # empty storage_class (default) -> FileMemoryStorage directly, no importlib (portable, zero noise)
    dm = DeerMem(backend_config=None)
    assert dm._config.storage_class == ""
    assert isinstance(dm._storage, FileMemoryStorage)


def test_portability_only_abc_contract_imports_deerflow():
    """backends/deermem/ has exactly ONE `from deerflow` line: the ABC contract in deer_mem.py."""
    import deerflow.agents.memory.backends.deermem as pkg

    root = Path(pkg.__file__).parent
    deerflow_imports = []
    for p in root.rglob("*.py"):
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("from deerflow") or s.startswith("import deerflow"):
                deerflow_imports.append((p.relative_to(root).as_posix(), s))
    assert len(deerflow_imports) == 1, deerflow_imports
    assert deerflow_imports[0][0] == "deer_mem.py"
    assert "memory.manager import MemoryManager" in deerflow_imports[0][1]


# Minimal vendored host contract (what another agent would ship). DeerMem only
# needs this ABC -- nothing else from a host.
_VENDORED_MANAGER_PY = '''
"""Vendored host contract (minimal ABC) for the portability demo."""
from abc import ABC, abstractmethod
from typing import Any

class MemoryManager(ABC):
    def __init__(self, backend_config: dict | None = None) -> None:
        self._backend_config = backend_config
    @abstractmethod
    def add(self, thread_id, messages, *, agent_name=None, user_id=None, trace_id=None) -> None: ...
    @abstractmethod
    def add_nowait(self, thread_id, messages, *, agent_name=None, user_id=None) -> None: ...
    @abstractmethod
    def get_context(self, user_id, *, agent_name=None, thread_id=None) -> str: ...
    @abstractmethod
    def search(self, query, top_k=5, *, user_id=None, agent_name=None) -> list: ...
    @abstractmethod
    def get_memory(self, *, user_id=None, agent_name=None) -> dict: ...
    @abstractmethod
    def delete_memory(self, *, user_id=None, agent_name=None) -> None: ...
    @abstractmethod
    def clear_memory(self, *, user_id=None, agent_name=None) -> dict: ...
    @abstractmethod
    def import_memory(self, memory_data, *, user_id=None, agent_name=None) -> dict: ...
    @abstractmethod
    def export_memory(self, *, user_id=None, agent_name=None) -> dict: ...
'''


def test_portability_vendor_to_other_agent(tmp_path, monkeypatch):
    """Copy backends/deermem/ into a temp package, repoint the ONE ABC import to
    a vendored manager, import, and run a round-trip -- proves copy + 1-line +
    run portability (zero deerflow dependency at runtime)."""
    import importlib
    import shutil

    import deerflow.agents.memory.backends.deermem as pkg

    src = Path(pkg.__file__).parent
    # Vendored host package with a minimal manager.py (the contract).
    host_pkg = tmp_path / "otheragent"
    host_pkg.mkdir()
    (host_pkg / "__init__.py").write_text("", encoding="utf-8")
    (host_pkg / "manager.py").write_text(_VENDORED_MANAGER_PY, encoding="utf-8")
    # Copy the DeerMem backend folder.
    dst_pkg = tmp_path / "otheragent_deermem"
    shutil.copytree(src, dst_pkg)
    # Repoint the single ABC-contract import line to the vendored manager.
    deer_mem_file = dst_pkg / "deer_mem.py"
    text = deer_mem_file.read_text(encoding="utf-8")
    assert "from deerflow.agents.memory.manager import MemoryManager" in text
    text = text.replace(
        "from deerflow.agents.memory.manager import MemoryManager",
        "from otheragent.manager import MemoryManager",
    )
    deer_mem_file.write_text(text, encoding="utf-8")

    monkeypatch.setenv("DEERMEM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mod = importlib.import_module("otheragent_deermem.deer_mem")
        assert hasattr(mod, "DeerMem")
        dm = mod.DeerMem(backend_config=None)  # zero config, self._llm=None
        dm.import_memory(
            {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": [{"id": "f", "content": "y", "category": "c", "confidence": 0.5, "createdAt": "", "source": "m"}]},
            user_id="ua",
        )
        assert "y" in dm.get_context(user_id="ua")
    finally:
        for k in [k for k in list(sys.modules) if k.startswith("otheragent_deermem") or k == "otheragent"]:
            sys.modules.pop(k, None)


def test_per_user_memory_path_matches_host_safe_user_id(deermem_data_dir):
    """Pin the per-user memory path across the abstraction.

    DeerMem writes memory to ``{storage_path}/users/{safe_user_id}/memory.json``
    where ``safe_user_id`` is byte-identical to the host's ``make_safe_user_id``.
    The factory injects ``runtime_home()`` (= base_dir) as ``storage_path``, so
    the on-disk path is ``{base_dir}/users/{uid}/memory.json`` -- identical to
    pre-abstraction. This locks that equivalence so a future change to DeerMem's
    path / safe_user_id logic can't silently orphan existing per-user memory
    (risk:high, persistent state).
    """
    from deerflow.config.paths import make_safe_user_id

    user_id = "test-user-123@example.com"
    # storage_path mirrors what the host factory injects (runtime_home / base_dir)
    dm = DeerMem(backend_config={"storage_path": str(deermem_data_dir)})
    dm.create_fact("User prefers concise answers", category="preference", user_id=user_id)

    expected_safe = make_safe_user_id(user_id)
    expected_file = deermem_data_dir / "users" / expected_safe / "memory.json"
    assert expected_file.is_file(), f"memory not at expected per-user path: {expected_file}"
    # DeerMem used the host-identical safe_user_id (not some other encoding).
    user_dirs = [p.name for p in (deermem_data_dir / "users").iterdir() if p.is_dir()]
    assert user_dirs == [expected_safe], f"safe_user_id diverged from host: {user_dirs}"
