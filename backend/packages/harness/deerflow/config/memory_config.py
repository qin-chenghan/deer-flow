"""Configuration for the memory mechanism (host-shared fields only).

DeerMem-private fields live in ``backends/deermem/config.py`` (``DeerMemConfig``),
reached via ``backend_config`` (a dict the factory passes to the backend's
``__init__``). This module holds ONLY the host-shared fields every backend /
call site / factory reads: ``enabled`` / ``injection_enabled`` /
``manager_class`` / ``backend_config``. Keeping the shared schema slim is what
makes backends swappable and portable (DeerMem's knobs do not leak onto the
shared contract).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MemoryConfig(BaseModel):
    """Host-shared memory configuration (backend-agnostic)."""

    enabled: bool = Field(
        default=True,
        description="Whether to enable the memory mechanism (call-site gate).",
    )
    mode: Literal["middleware", "tool"] = Field(
        default="middleware",
        description=(
            "Memory operation mode. 'middleware': passive LLM summarization after each turn (current behavior). 'tool': model calls memory tools (memory_search, memory_add, etc.) directly. Mutually exclusive — only one mode runs at a time."
        ),
    )
    injection_enabled: bool = Field(
        default=True,
        description="Whether to inject memory into the system prompt (call-site gate).",
    )
    manager_class: str = Field(
        default="deermem",
        description=(
            "Memory backend selector. Either a registered backend name "
            "(matching a `backends/<name>/` folder that exposes `MANAGER_CLASS`, "
            "e.g. `deermem` / `noop`) or a dotted import path to a "
            "`MemoryManager` subclass. The factory resolves this at "
            "`get_memory_manager()` time and raises `ValueError` on failure "
            "(fail-fast: memory is persistent state, so an unresolved "
            "manager_class is not silently substituted with a different "
            "storage backend)."
        ),
    )
    backend_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Backend-private config (a dict), passed verbatim to the backend's "
            "`__init__(backend_config=...)` by the factory. Each backend "
            "self-interprets it (DeerMem parses it into `DeerMemConfig`). Values "
            "live in the host config file (`config.yaml` `memory.backend_config`); "
            "they do not belong on the shared `MemoryConfig` schema."
        ),
    )


def should_use_memory_tools(config: MemoryConfig) -> bool:
    """Return True when memory should use model-directed tools."""
    return config.enabled and config.mode == "tool"


# Global configuration instance
_memory_config: MemoryConfig = MemoryConfig()


def get_memory_config() -> MemoryConfig:
    """Get the current memory configuration."""
    return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    """Set the memory configuration."""
    global _memory_config
    _memory_config = config


def load_memory_config_from_dict(config_dict: dict) -> None:
    """Load memory configuration from a dictionary.

    DeerMem-private keys (from a not-yet-migrated config) are ignored -- they
    must live under ``backend_config`` to reach the backend.
    """
    global _memory_config
    _memory_config = MemoryConfig(**config_dict)
