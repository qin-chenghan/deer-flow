"""Noop memory backend -- a functional empty :class:`MemoryManager`.

Proves the pluggable mechanism end-to-end (factory + drop-in discovery + config
switch) and doubles as the template for a new backend:

1. Copy this folder to ``backends/<yourname>/``.
2. Rename the class, implement the 9 methods against your memory system.
3. Set ``MANAGER_CLASS`` in ``<yourname>/__init__.py``.
4. Set ``manager_class: <yourname>`` in ``config.yaml``.

With ``manager_class: noop`` the system runs with an empty memory: nothing is
stored, nothing is injected, every read returns empty. Useful for tests, for
disabling memory without touching ``enabled``, and as a baseline.
"""

from __future__ import annotations

from typing import Any

from deerflow.agents.memory.manager import MemoryManager


def _empty_memory() -> dict[str, Any]:
    """A fresh empty memory document (callers may mutate)."""
    return {"facts": []}


class NoopMemoryManager(MemoryManager):
    """Backend that stores and recalls nothing."""

    # ── Write ────────────────────────────────────────────────────────────
    def add(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        return None

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> None:
        return None

    # ── Read ─────────────────────────────────────────────────────────────
    def get_context(
        self,
        user_id: str | None,
        *,
        agent_name: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        return ""

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    # ── Manage ───────────────────────────────────────────────────────────
    def get_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        return _empty_memory()

    def delete_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        return None

    def clear_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        return _empty_memory()

    def import_memory(
        self,
        memory_data: dict[str, Any],
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        return _empty_memory()

    def export_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        return _empty_memory()
