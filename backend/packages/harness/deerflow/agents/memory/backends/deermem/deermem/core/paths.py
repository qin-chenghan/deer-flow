"""DeerMem's own storage path resolution (no deer-flow ``get_paths`` / ``AGENT_NAME_PATTERN``).

The host no longer dictates where DeerMem stores data. Root = ``config.storage_path``
(if set, absolute or relative) or ``$DEERMEM_DATA_DIR`` or ``~/.deermem/``.
Per-user / per-agent / legacy layouts live under the root, mirroring the
pre-abstraction paths so a one-time data migration (old ``{base_dir}/users/*``
-> DeerMem root) is a plain move.

user_id is sanitized in-process (``[A-Za-z0-9_-]`` + SHA-256 digest for lossy
ids) and agent_name validated against an inlined pattern -- DeerMem does not
import the host's ``make_safe_user_id`` / ``_validate_user_id`` /
``AGENT_NAME_PATTERN``.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import DeerMemConfig

# user_id charset + sanitization (mirrors the host's make_safe_user_id so
# existing per-user buckets line up after migration).
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_UNSAFE_USER_ID_CHAR_RE = re.compile(r"[^A-Za-z0-9_\-]")
_SAFE_USER_ID_DIGEST_HEX_LEN = 16

# agent_name validation (inlined; was deer-flow's AGENT_NAME_PATTERN).
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def safe_user_id(raw: str) -> str:
    """Normalize an external identity into the user-id charset (``[A-Za-z0-9_-]``).

    Idempotent: already-safe ids pass through; lossy ones get a short SHA-256
    digest suffix so two distinct inputs never share a bucket. Mirrors the
    host's ``make_safe_user_id`` so existing per-user buckets line up after
    migration.
    """
    if not raw:
        raise ValueError("user_id must be a non-empty string.")
    sanitized = _UNSAFE_USER_ID_CHAR_RE.sub("-", raw)
    if sanitized == raw:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SAFE_USER_ID_DIGEST_HEX_LEN]
    return f"{sanitized}-{digest}"


def validate_agent_name(name: str) -> None:
    """Validate that the agent name is safe to use in filesystem paths."""
    if not name:
        raise ValueError("Agent name must be a non-empty string.")
    if not AGENT_NAME_PATTERN.match(name):
        raise ValueError(f"Invalid agent name {name!r}: names must match {AGENT_NAME_PATTERN.pattern}")


def validate_project_id(project_id: str) -> None:
    """Validate the trusted internal project id used for storage bucketing."""
    if not project_id:
        raise ValueError("Project id must be a non-empty string.")
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ValueError(f"Invalid project id {project_id!r}: ids must match {PROJECT_ID_PATTERN.pattern}")


def _default_root() -> Path:
    """DeerMem's default data root: ``$DEERMEM_DATA_DIR`` or ``~/.deermem/``."""
    env = os.environ.get("DEERMEM_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".deermem"


def memory_file_path(
    config: DeerMemConfig,
    agent_name: str | None = None,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
) -> Path:
    """Resolve the memory file path under DeerMem's own data root.

    ``config.storage_path`` (absolute or relative) is the root; per-user /
    per-agent / legacy layouts live under it. Empty -> default root
    (``$DEERMEM_DATA_DIR`` / ``~/.deermem/``). The host (deer-flow factory)
    injects an absolute base_dir as ``storage_path`` so memory lands at
    ``{base_dir}/users/{user_id}/memory.json`` (CWD-independent).
    """
    root = Path(config.storage_path) if config.storage_path else _default_root()
    if config.strict_user_scope and user_id is None:
        raise ValueError("user_id is required when strict_user_scope is enabled.")
    manifest_filename = config.manifest_filename
    if Path(manifest_filename).name != manifest_filename or not manifest_filename.endswith(".json"):
        raise ValueError("manifest_filename must be a plain .json filename.")

    if user_id is not None:
        uid = safe_user_id(user_id)
        if agent_name is not None:
            validate_agent_name(agent_name)
            bucket = root / "users" / uid / "agents" / agent_name.lower()
        else:
            bucket = root / "users" / uid
        if project_id is not None:
            validate_project_id(project_id)
            bucket = bucket / "projects" / project_id
        return bucket / manifest_filename
    # Legacy: no user_id
    if agent_name is not None:
        validate_agent_name(agent_name)
        bucket = root / "agents" / agent_name.lower()
    else:
        bucket = root
    if project_id is not None:
        validate_project_id(project_id)
        bucket = bucket / "projects" / project_id
    return bucket / manifest_filename


def fact_file_path(manifest_path: Path, fact_id: str) -> Path:
    """Return the sharded Markdown path for a fact under a scope bucket."""
    if not fact_id or not re.fullmatch(r"[A-Za-z0-9_-]+", fact_id):
        raise ValueError("Fact id may contain only letters, numbers, '_' and '-'.")
    prefix = fact_id[:2].lower() if len(fact_id) >= 2 else "__"
    return manifest_path.parent / "facts" / prefix / f"{fact_id}.md"
