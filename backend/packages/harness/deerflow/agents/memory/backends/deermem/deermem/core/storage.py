"""Memory storage providers.

The file backend stores only project-independent user/history summaries in one
user-level ``memory.json``. Each fact is canonical in one Markdown file below
its required agent name. The
public ``load``/``save`` compatibility surface still exposes the historical
document shape (``facts`` is a list), so updater and gateway callers can move
to the fact repository API incrementally.
"""

from __future__ import annotations

import abc
import copy
import hashlib
import importlib
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from ..config import DeerMemConfig
from .paths import agent_facts_directory, fact_file_path, memory_file_path

logger = logging.getLogger(__name__)

DOCUMENT_VERSION = "2.0"
CORE_CATEGORIES = frozenset({"preference", "correction", "context", "goal", "behavior", "identity", "constraint", "decision", "other"})


class MemoryStorageError(RuntimeError):
    """Base error for persistent memory failures."""


class MemoryStorageCorruption(MemoryStorageError):
    """The global memory JSON or a canonical fact cannot be parsed safely."""


class MemoryRevisionConflict(MemoryStorageError):
    """A stale writer attempted to overwrite a newer user-memory revision."""


class RetrievalPort(Protocol):
    """Storage-facing adapter implemented by the independent retrieval module."""

    def upsert(self, fact: dict[str, Any], *, scope: dict[str, str | None], path: str) -> None: ...

    def remove(self, fact_id: str, *, scope: dict[str, str | None]) -> None: ...

    def search(self, query: str, *, scopes: list[dict[str, str | None]], top_k: int, mode: str, filters: dict[str, Any] | None) -> list[dict[str, Any]]: ...


def utc_now_iso_z() -> str:
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory() -> dict[str, Any]:
    """Return the compatibility document shape used by updater/injection."""
    return {
        "version": "1.0",
        "revision": 0,
        "lastUpdated": utc_now_iso_z(),
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


def _scope_dict(user_id: str | None, agent_name: str | None) -> dict[str, str | None]:
    return {"userId": user_id, "agentName": agent_name}


def _content_hash(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _file_signature(path: Path) -> tuple[int, int] | None:
    """Use nanosecond mtime plus size so cache validation is not mtime-only."""
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _normalize_category(fact: dict[str, Any]) -> None:
    category = str(fact.get("category") or "context")
    if category not in CORE_CATEGORIES:
        fact.setdefault("categoryExtension", category)
        fact["category"] = "other"


def _normalize_fact(fact: dict[str, Any], *, scope: dict[str, str | None]) -> dict[str, Any]:
    normalized = copy.deepcopy(fact)
    normalized["id"] = str(normalized.get("id") or f"fact_{uuid.uuid4().hex}")
    normalized["schemaVersion"] = 2
    normalized["content"] = str(normalized.get("content") or "").strip()
    _normalize_category(normalized)
    try:
        confidence = float(normalized.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    normalized["confidence"] = confidence if math.isfinite(confidence) and 0 <= confidence <= 1 else 0.5
    normalized["status"] = "active"
    normalized["scope"] = copy.deepcopy(scope)
    normalized.setdefault("createdAt", utc_now_iso_z())
    normalized.setdefault("updatedAt", normalized["createdAt"])
    normalized["revision"] = max(1, int(normalized.get("revision") or 1))
    source = normalized.get("source")
    if isinstance(source, str):
        if source in {"manual", "consolidation", "import", "unknown"}:
            normalized["source"] = {"type": source, "threadId": None}
        else:
            normalized["source"] = {"type": "conversation", "threadId": source}
    elif not isinstance(source, dict):
        normalized["source"] = {"type": "unknown", "threadId": None}
    else:
        normalized["source"].setdefault("type", "unknown")
    normalized.setdefault("topics", [])
    normalized.setdefault("consolidatedFrom", [])
    if normalized["consolidatedFrom"]:
        normalized.setdefault("consolidatedAt", normalized["updatedAt"])
    return normalized


def _fact_title(fact: dict[str, Any]) -> str:
    explicit = str(fact.get("title") or "").strip()
    if explicit:
        return explicit.replace("\n", " ")[:160]
    first = str(fact.get("content") or "Memory fact").splitlines()[0].strip()
    return (first or "Memory fact")[:160]


def _render_fact_markdown(fact: dict[str, Any]) -> bytes:
    metadata = {key: copy.deepcopy(value) for key, value in fact.items() if key not in {"content", "title"}}
    scope = metadata.pop("scope", {})
    if isinstance(scope, dict):
        metadata["user_id"] = scope.get("userId")
        metadata["agent_name"] = scope.get("agentName")
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    text = f"---\n{front_matter}\n---\n\n# {_fact_title(fact)}\n\n{fact['content'].rstrip()}\n"
    return text.encode("utf-8")


def _parse_fact_markdown(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError("missing YAML front matter")
        front, body = text[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(front) or {}
        if not isinstance(metadata, dict):
            raise ValueError("front matter is not a mapping")
        body = body.lstrip("\n")
        lines = body.splitlines()
        title = ""
        if lines and lines[0].startswith("# "):
            title = lines.pop(0)[2:].strip()
            if lines and not lines[0].strip():
                lines.pop(0)
        metadata["title"] = title
        metadata["content"] = "\n".join(lines).rstrip("\n")
        metadata["scope"] = {
            "userId": metadata.pop("user_id", None),
            "agentName": metadata.pop("agent_name", None),
        }
        return metadata
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise MemoryStorageCorruption(f"Failed to parse canonical fact {path}: {exc}") from exc


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _process_file_lock(lock_path: Path, timeout_seconds: float) -> Iterator[None]:
    """Cross-process advisory lock for one scope, using only the stdlib."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring memory scope lock {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.warning("Failed to release memory scope lock %s", lock_path)
        handle.close()


class MemoryStorage(abc.ABC):
    @abc.abstractmethod
    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def save(
        self,
        memory_data: dict[str, Any],
        agent_name: str | None = None,
        *,
        user_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool: ...

    def apply_changes(self, change_set: dict[str, Any], **scope: Any) -> dict[str, Any]:
        """Apply one repository change set; providers may override atomically."""
        raise NotImplementedError


class FileMemoryStorage(MemoryStorage):
    def __init__(self, config: DeerMemConfig, retrieval: RetrievalPort | None = None):
        self._config = config
        self._retrieval = retrieval
        self._memory_cache: dict[tuple[str | None, str | None], tuple[dict[str, Any], tuple[Any, ...]]] = {}
        self._cache_lock = threading.Lock()
        self._scope_locks: dict[tuple[str | None, str | None], threading.RLock] = {}

    @staticmethod
    def _cache_key(agent_name: str | None = None, *, user_id: str | None = None) -> tuple[str | None, str | None]:
        return (user_id, agent_name)

    def _scope_lock(self, key: tuple[str | None, str | None]) -> threading.RLock:
        with self._cache_lock:
            return self._scope_locks.setdefault(key, threading.RLock())

    def _get_memory_file_path(self, agent_name: str | None = None, *, user_id: str | None = None) -> Path:
        return memory_file_path(self._config, agent_name, user_id=user_id)

    def _scope_signature(self, path: Path, agent_name: str | None) -> tuple[Any, ...]:
        """Track the global summary file plus the selected agent's fact files."""
        signature: list[Any] = [_file_signature(path)]
        if agent_name is not None:
            fact_root = agent_facts_directory(path, agent_name)
            for fact_path in sorted(fact_root.glob("**/*.md")):
                file_signature = _file_signature(fact_path)
                if file_signature is not None:
                    signature.append((fact_path.as_posix(), *file_signature))
        return tuple(signature)

    def _load_agent_facts(self, path: Path, agent_name: str | None) -> list[dict[str, Any]]:
        if agent_name is None:
            return []
        facts: list[dict[str, Any]] = []
        for fact_path in sorted(agent_facts_directory(path, agent_name).glob("**/*.md")):
            fact = _parse_fact_markdown(fact_path)
            if str(fact.get("id")) != fact_path.stem:
                raise MemoryStorageCorruption(f"Fact id mismatch for {fact_path}")
            facts.append(fact)
        return facts

    def _agent_entries(self, path: Path, agent_name: str | None) -> dict[str, dict[str, str]]:
        if agent_name is None:
            return {}
        entries: dict[str, dict[str, str]] = {}
        for fact_path in sorted(agent_facts_directory(path, agent_name).glob("**/*.md")):
            fact = _parse_fact_markdown(fact_path)
            fact_id = str(fact.get("id") or "")
            if not fact_id or fact_id != fact_path.stem:
                raise MemoryStorageCorruption(f"Fact id mismatch for {fact_path}")
            entries[fact_id] = {"path": fact_path.relative_to(path.parent).as_posix()}
        return entries

    def _legacy_agent_memory_path(self, path: Path, agent_name: str) -> Path:
        return path.parent / "agents" / agent_name.lower() / path.name

    def _migrate_legacy_agent_file(self, path: Path, agent_name: str, *, user_id: str | None) -> bool:
        """Move facts out of the former per-agent memory.json on first access."""
        legacy_path = self._legacy_agent_memory_path(path, agent_name)
        if not legacy_path.exists():
            return False
        legacy_memory = self._load_memory_file(legacy_path)
        if legacy_memory is None:
            return False
        document = self._document_from_memory_file(legacy_memory, legacy_path, agent_name)
        global_memory = self._load_memory_file(path)
        if global_memory is not None:
            document["user"] = copy.deepcopy(global_memory.get("user", {}))
            document["history"] = copy.deepcopy(global_memory.get("history", {}))
        document["revision"] = int((global_memory or {}).get("revision") or 0)
        if not self.save(document, agent_name, user_id=user_id, expected_revision=document["revision"]):
            raise MemoryStorageError(f"Failed to migrate legacy agent memory {legacy_path}")
        legacy_path.unlink(missing_ok=True)
        return True

    def _load_memory_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as exc:
            raise MemoryStorageCorruption(f"Failed to load global memory JSON {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise MemoryStorageCorruption(f"Global memory JSON {path} is not an object")
        return value

    def _recover_if_needed(self, path: Path) -> None:
        """Recover or clean a previously journaled multi-file operation.

        Callers hold the scope's in-process and cross-process locks.
        """
        journal_path = path.parent / ".memory.journal.json"
        if not journal_path.exists():
            return
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            operation_id = str(journal["operationId"])
            state = journal.get("state")
            old_entries = journal.get("oldEntries", {})
            agent_name = journal.get("agentName")
            if agent_name is not None and not isinstance(agent_name, str):
                raise TypeError("agentName must be a string or null")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise MemoryStorageCorruption(f"Invalid memory operation journal {journal_path}: {exc}") from exc
        recovery_dir = path.parent / ".recovery" / operation_id
        if state == "prepared":
            backup_manifest = recovery_dir / "memory.json"
            if backup_manifest.exists():
                _atomic_write(path, backup_manifest.read_bytes())
            elif int(journal.get("expectedRevision") or 0) == 0:
                path.unlink(missing_ok=True)
            if isinstance(old_entries, dict):
                old_ids = set(old_entries)
                for fact_id in journal.get("factIds", []):
                    if fact_id not in old_ids:
                        if agent_name is None:
                            raise MemoryStorageCorruption(f"Journal {journal_path} contains facts without agentName")
                        fact_file_path(path, str(fact_id), agent_name=agent_name).unlink(missing_ok=True)
                for fact_id, entry in old_entries.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        continue
                    backup = recovery_dir / f"{fact_id}.md"
                    if backup.exists():
                        _atomic_write(path.parent / entry["path"], backup.read_bytes())
        elif state != "committed":
            raise MemoryStorageCorruption(f"Unknown journal state {state!r} in {journal_path}")
        if recovery_dir.exists():
            shutil.rmtree(recovery_dir)
        journal_path.unlink(missing_ok=True)

    def _document_from_memory_file(self, memory_file: dict[str, Any], path: Path, agent_name: str | None) -> dict[str, Any]:
        """Build the compatibility document without persisting facts in JSON."""
        legacy_facts = memory_file.get("facts")
        if isinstance(legacy_facts, list):
            facts = copy.deepcopy(legacy_facts) if agent_name is not None else []
        elif isinstance(legacy_facts, dict):
            facts = []
            if agent_name is not None:
                for fact_id, entry in legacy_facts.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        raise MemoryStorageCorruption(f"Invalid legacy manifest entry for fact {fact_id!r}")
                    fact_path = path.parent / entry["path"]
                    fact = _parse_fact_markdown(fact_path)
                    if entry.get("contentHash") and entry.get("contentHash") != _content_hash(fact_path.read_bytes()):
                        raise MemoryStorageCorruption(f"Hash mismatch for canonical fact {fact_id!r}")
                    facts.append(fact)
        elif legacy_facts is None:
            facts = self._load_agent_facts(path, agent_name)
        else:
            raise MemoryStorageCorruption(f"Legacy facts in {path} must be a list or mapping")
        result = {key: copy.deepcopy(value) for key, value in memory_file.items() if key != "facts"}
        result.setdefault("revision", 0)
        result["facts"] = facts
        return result

    def _read_document(self, path: Path, agent_name: str | None) -> dict[str, Any]:
        memory_file = self._load_memory_file(path)
        if memory_file is None:
            result = create_empty_memory()
            result["facts"] = self._load_agent_facts(path, agent_name)
            return result
        return self._document_from_memory_file(memory_file, path, agent_name)

    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        path = self._get_memory_file_path(agent_name, user_id=user_id)
        key = self._cache_key(agent_name, user_id=user_id)
        if agent_name is not None:
            self._migrate_legacy_agent_file(path, agent_name, user_id=user_id)
        journal_path = path.parent / ".memory.journal.json"
        if journal_path.exists():
            with self._scope_lock(key), _process_file_lock(path.parent / ".memory.lock", float(getattr(self._config, "file_lock_timeout_seconds", 10))):
                self._recover_if_needed(path)
        signature = self._scope_signature(path, agent_name)
        with self._cache_lock:
            cached = self._memory_cache.get(key)
            if cached is not None and cached[1] == signature:
                return copy.deepcopy(cached[0])
        memory_file = self._load_memory_file(path)
        document = self._read_document(path, agent_name)
        needs_migration = memory_file is not None and (memory_file.get("version") != DOCUMENT_VERSION or "facts" in memory_file)
        if agent_name is not None and needs_migration:
            try:
                if not self.save(document, agent_name, user_id=user_id, expected_revision=int(document.get("revision") or 0)):
                    raise MemoryStorageError(f"Failed to migrate legacy memory document {path}")
            except MemoryRevisionConflict:
                # Another reader completed the one-time migration first.
                pass
            document = self._read_document(path, agent_name)
            signature = self._scope_signature(path, agent_name)
        with self._cache_lock:
            self._memory_cache[key] = (copy.deepcopy(document), signature)
        return copy.deepcopy(document)

    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        path = self._get_memory_file_path(agent_name, user_id=user_id)
        key = self._cache_key(agent_name, user_id=user_id)
        if agent_name is not None:
            self._migrate_legacy_agent_file(path, agent_name, user_id=user_id)
        if (path.parent / ".memory.journal.json").exists():
            with self._scope_lock(key), _process_file_lock(path.parent / ".memory.lock", float(getattr(self._config, "file_lock_timeout_seconds", 10))):
                self._recover_if_needed(path)
        memory_file = self._load_memory_file(path)
        document = self._read_document(path, agent_name)
        needs_migration = memory_file is not None and (memory_file.get("version") != DOCUMENT_VERSION or "facts" in memory_file)
        if agent_name is not None and needs_migration:
            try:
                if not self.save(document, agent_name, user_id=user_id, expected_revision=int(document.get("revision") or 0)):
                    raise MemoryStorageError(f"Failed to migrate legacy memory document {path}")
            except MemoryRevisionConflict:
                # Another reader completed the one-time migration first.
                pass
            document = self._read_document(path, agent_name)
        signature = self._scope_signature(path, agent_name)
        with self._cache_lock:
            self._memory_cache[key] = (copy.deepcopy(document), signature)
        return copy.deepcopy(document)

    def migrate(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        """Run the idempotent version-driven migration for one exact scope."""
        if agent_name is None:
            raise ValueError("agent_name is required to migrate legacy facts")
        before_path = self._get_memory_file_path(agent_name, user_id=user_id)
        before = self._load_memory_file(before_path)
        needed = before is not None and (before.get("version") != DOCUMENT_VERSION or "facts" in before)
        document = self.reload(agent_name, user_id=user_id)
        return {
            "migrated": needed,
            "fromVersion": None if before is None else before.get("version"),
            "toVersion": document.get("version"),
            "revision": document.get("revision", 0),
        }

    def save(
        self,
        memory_data: dict[str, Any],
        agent_name: str | None = None,
        *,
        user_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        path = self._get_memory_file_path(agent_name, user_id=user_id)
        key = self._cache_key(agent_name, user_id=user_id)
        lock_path = path.parent / ".memory.lock"
        journal_path = path.parent / ".memory.journal.json"
        scope = _scope_dict(user_id, agent_name)
        notifications: list[tuple[str, dict[str, Any] | str, str | None]] = []
        try:
            with self._scope_lock(key), _process_file_lock(lock_path, float(getattr(self._config, "file_lock_timeout_seconds", 10))):
                self._recover_if_needed(path)
                current_memory_file = self._load_memory_file(path)
                current_revision = int((current_memory_file or {}).get("revision") or 0)
                if expected_revision is not None and expected_revision != current_revision:
                    raise MemoryRevisionConflict(f"Expected user-memory revision {expected_revision}, found {current_revision}")
                facts_raw = memory_data.get("facts", [])
                if not isinstance(facts_raw, list):
                    raise ValueError("memory_data.facts must be a list")
                if agent_name is None and facts_raw:
                    raise ValueError("agent_name is required to persist facts")
                old_entries = self._agent_entries(path, agent_name)
                facts = [_normalize_fact(fact, scope=scope) for fact in facts_raw if isinstance(fact, dict)] if agent_name is not None else []
                ids = [fact["id"] for fact in facts]
                if len(ids) != len(set(ids)):
                    raise ValueError("Duplicate fact ids are not allowed")
                next_revision = current_revision + 1
                journal = {
                    "operationId": uuid.uuid4().hex,
                    "state": "prepared",
                    "agentName": agent_name,
                    "expectedRevision": current_revision,
                    "nextRevision": next_revision,
                    "factIds": ids,
                    "oldEntries": copy.deepcopy(old_entries),
                }
                recovery_dir = path.parent / ".recovery" / journal["operationId"]
                if current_memory_file is not None or old_entries:
                    recovery_dir.mkdir(parents=True, exist_ok=True)
                    if current_memory_file is not None:
                        shutil.copy2(path, recovery_dir / "memory.json")
                    for old_fact_id, old_entry in old_entries.items():
                        if isinstance(old_entry, dict) and isinstance(old_entry.get("path"), str):
                            old_fact_path = path.parent / old_entry["path"]
                            if old_fact_path.exists():
                                shutil.copy2(old_fact_path, recovery_dir / f"{old_fact_id}.md")
                _atomic_write(journal_path, json.dumps(journal, ensure_ascii=False, indent=2).encode("utf-8"))

                entries: dict[str, Any] = {}
                for fact in facts:
                    if agent_name is None:  # guarded above; keeps the type checker honest
                        raise ValueError("agent_name is required to persist facts")
                    fact_path = fact_file_path(path, fact["id"], agent_name=agent_name)
                    raw = _render_fact_markdown(fact)
                    _atomic_write(fact_path, raw)
                    entries[fact["id"]] = {
                        "path": fact_path.relative_to(path.parent).as_posix(),
                    }
                    notifications.append(("upsert", fact, str(fact_path)))

                removed = set(old_entries) - set(entries)
                for fact_id in removed:
                    entry = old_entries.get(fact_id)
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                        old_path = path.parent / entry["path"]
                        if old_path.exists():
                            old_path.unlink()
                    notifications.append(("remove", fact_id, None))

                if agent_name is None:
                    user_section = copy.deepcopy(memory_data.get("user", {}))
                    history_section = copy.deepcopy(memory_data.get("history", {}))
                else:
                    # Agent conversations may contain project-specific summaries.
                    # Only a global (agent_name=None) write may change user/history.
                    base = current_memory_file or create_empty_memory()
                    user_section = copy.deepcopy(base.get("user", {}))
                    history_section = copy.deepcopy(base.get("history", {}))
                memory_file = {
                    "version": DOCUMENT_VERSION,
                    "revision": next_revision,
                    "lastUpdated": utc_now_iso_z(),
                    "user": user_section,
                    "history": history_section,
                }
                _atomic_write(path, json.dumps(memory_file, ensure_ascii=False, indent=2).encode("utf-8"))
                journal["state"] = "committed"
                _atomic_write(journal_path, json.dumps(journal, ensure_ascii=False, indent=2).encode("utf-8"))
                if recovery_dir.exists():
                    shutil.rmtree(recovery_dir)
                journal_path.unlink(missing_ok=True)
                document = self._document_from_memory_file(memory_file, path, agent_name)
                signature = self._scope_signature(path, agent_name)
                with self._cache_lock:
                    self._memory_cache[key] = (copy.deepcopy(document), signature)
        except MemoryRevisionConflict:
            raise
        except (OSError, ValueError, MemoryStorageCorruption) as exc:
            logger.error("Failed to save memory scope %s: %s", key, exc)
            return False

        if self._retrieval is not None:
            for action, value, fact_path in notifications:
                try:
                    if action == "upsert":
                        self._retrieval.upsert(value, scope=scope, path=fact_path or "")
                    else:
                        self._retrieval.remove(str(value), scope=scope)
                except Exception:
                    logger.exception("Retrieval notification failed for %s", value)
        return True

    @staticmethod
    def _scope_kwargs(scope: dict[str, str | None]) -> dict[str, str]:
        kwargs: dict[str, str] = {}
        if scope.get("userId") is not None:
            kwargs["user_id"] = str(scope["userId"])
        if scope.get("agentName") is not None:
            kwargs["agent_name"] = str(scope["agentName"])
        return kwargs

    def get_fact(
        self,
        fact_id: str,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any] | None:
        for fact in self.load(agent_name, user_id=user_id).get("facts", []):
            if str(fact.get("id")) == fact_id:
                return copy.deepcopy(fact)
        return None

    def list_facts(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        filters: dict[str, Any] | None = None,
        cursor: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if cursor < 0 or limit < 1:
            raise ValueError("cursor must be >= 0 and limit must be >= 1")
        facts = self.load(agent_name, user_id=user_id).get("facts", [])
        filters = filters or {}
        matched = [fact for fact in facts if all(key in fact and fact.get(key) == value for key, value in filters.items())]
        return copy.deepcopy(matched[cursor : cursor + limit])

    def apply_changes(
        self,
        change_set: dict[str, Any],
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        expected_manifest_revision: int | None = None,
    ) -> dict[str, Any]:
        """Commit summary/fact changes through the same journaled save path."""
        has_fact_changes = bool(change_set.get("upserts") or change_set.get("deletes"))
        if has_fact_changes and agent_name is None:
            raise ValueError("agent_name is required for fact repository changes")
        document = self.load(agent_name, user_id=user_id)
        by_id = {str(fact.get("id")): copy.deepcopy(fact) for fact in document.get("facts", [])}
        for fact_id in change_set.get("deletes", []):
            by_id.pop(str(fact_id), None)
        for fact in change_set.get("upserts", []):
            if not isinstance(fact, dict):
                raise ValueError("change_set.upserts must contain fact objects")
            normalized_id = str(fact.get("id") or f"fact_{uuid.uuid4().hex}")
            fact = copy.deepcopy(fact)
            fact["id"] = normalized_id
            by_id[normalized_id] = fact
        summaries = change_set.get("summaries")
        if summaries is not None and agent_name is None:
            if not isinstance(summaries, dict):
                raise ValueError("change_set.summaries must be an object")
            for section in ("user", "history"):
                if section in summaries:
                    document[section] = copy.deepcopy(summaries[section])
        document["facts"] = list(by_id.values())
        expected = int(document.get("revision") or 0) if expected_manifest_revision is None else expected_manifest_revision
        if not self.save(document, agent_name, user_id=user_id, expected_revision=expected):
            raise MemoryStorageError("Failed to apply memory repository changes")
        return self.reload(agent_name, user_id=user_id)

    def upsert_fact(
        self,
        fact: dict[str, Any],
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if agent_name is None:
            raise ValueError("agent_name is required to upsert a fact")
        return self.apply_changes(
            {"upserts": [fact]},
            user_id=user_id,
            agent_name=agent_name,
            expected_manifest_revision=expected_revision,
        )

    def delete_fact(
        self,
        fact_id: str,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if agent_name is None:
            raise ValueError("agent_name is required to delete a fact")
        return self.apply_changes(
            {"deletes": [fact_id]},
            user_id=user_id,
            agent_name=agent_name,
            expected_manifest_revision=expected_revision,
        )

    def get_summaries(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        document = self.load(agent_name, user_id=user_id)
        return {"user": copy.deepcopy(document.get("user", {})), "history": copy.deepcopy(document.get("history", {})), "revision": document.get("revision", 0)}

    def update_summaries(
        self,
        summaries: dict[str, Any],
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        # Summaries are always user-global, never agent-specific.
        document = self.load(user_id=user_id)
        document.update({key: copy.deepcopy(value) for key, value in summaries.items() if key in {"user", "history"}})
        expected = int(document.get("revision") or 0) if expected_revision is None else expected_revision
        if not self.save(document, user_id=user_id, expected_revision=expected):
            raise MemoryStorageError("Failed to update global memory summaries")
        return self.reload(user_id=user_id)

    def notify_fact_upsert(self, fact: dict[str, Any], *, path: str = "") -> bool:
        if self._retrieval is None:
            return False
        scope = fact.get("scope") if isinstance(fact.get("scope"), dict) else {}
        self._retrieval.upsert(copy.deepcopy(fact), scope=copy.deepcopy(scope), path=path)
        return True

    def notify_fact_remove(self, fact_id: str, *, scope: dict[str, str | None]) -> bool:
        if self._retrieval is None:
            return False
        self._retrieval.remove(fact_id, scope=copy.deepcopy(scope))
        return True

    def search_facts(
        self,
        query: str,
        *,
        scopes: list[dict[str, str | None]],
        top_k: int = 10,
        mode: str = "hybrid",
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self._retrieval is not None:
            return self._retrieval.search(query, scopes=scopes, top_k=top_k, mode=mode, filters=filters)
        query_lower = query.strip().lower()
        if not query_lower or top_k <= 0:
            return []
        results: list[dict[str, Any]] = []
        for scope in scopes:
            facts = self.list_facts(filters=filters, **self._scope_kwargs(scope))
            for fact in facts:
                content = fact.get("content")
                if isinstance(content, str) and query_lower in content.lower():
                    results.append({"fact": fact, "score": float(fact.get("confidence") or 0.5), "matchType": "substring"})
        results.sort(key=lambda result: result["score"], reverse=True)
        return results[:top_k]

    def rebuild_index(self, scopes: list[dict[str, str | None]] | None = None) -> dict[str, Any]:
        if self._retrieval is None:
            return {"supported": False, "indexed": 0, "failed": 0, "reason": "retrieval_not_configured"}
        indexed = 0
        failed = 0
        if scopes is None:
            root = Path(self._config.storage_path) if self._config.storage_path else memory_file_path(self._config).parent
            candidates = root.glob("**/facts/**/*.md")
            for path in candidates:
                try:
                    fact = _parse_fact_markdown(path)
                    self.notify_fact_upsert(fact, path=str(path))
                    indexed += 1
                except (MemoryStorageError, OSError, ValueError):
                    failed += 1
        else:
            for scope in scopes:
                kwargs = self._scope_kwargs(scope)
                memory_path = self._get_memory_file_path(**kwargs)
                agent_name = kwargs.get("agent_name")
                if agent_name is None:
                    continue
                for fact in self.list_facts(**kwargs):
                    try:
                        self.notify_fact_upsert(fact, path=str(fact_file_path(memory_path, fact["id"], agent_name=agent_name)))
                        indexed += 1
                    except (OSError, ValueError):
                        failed += 1
        return {"supported": True, "indexed": indexed, "failed": failed}

    def retrieval_status(self) -> dict[str, Any]:
        return {
            "configured": self._retrieval is not None,
            "mode": "external" if self._retrieval is not None else "substring_fallback",
        }

    def capabilities(self) -> set[str]:
        capabilities = {"file", "markdown-facts", "global-summary-json", "revision", "journal", "fact-repository", "substring-fallback"}
        if self._retrieval is not None:
            capabilities.add("retrieval")
        return capabilities


def create_storage(config: DeerMemConfig, retrieval: RetrievalPort | None = None) -> MemoryStorage:
    if retrieval is None and config.retrieval_adapter:
        try:
            module_path, factory_name = config.retrieval_adapter.rsplit(".", 1)
            factory = getattr(importlib.import_module(module_path), factory_name)
            retrieval = factory(config)
        except Exception as exc:
            raise ValueError(f"backend_config.retrieval_adapter={config.retrieval_adapter!r} failed to load: {exc}") from exc
    storage_class_path = config.storage_class
    if not storage_class_path or storage_class_path == "file":
        return FileMemoryStorage(config, retrieval=retrieval)
    try:
        module_path, class_name = storage_class_path.rsplit(".", 1)
        storage_class = getattr(importlib.import_module(module_path), class_name)
        if not isinstance(storage_class, type) or not issubclass(storage_class, MemoryStorage):
            raise TypeError(f"Configured memory storage '{storage_class_path}' is not a MemoryStorage class")
        return storage_class(config)
    except Exception as exc:
        raise ValueError(f"backend_config.storage_class={storage_class_path!r} failed to load: {exc}. Refusing to silently fall back because memory is persistent state.") from exc
