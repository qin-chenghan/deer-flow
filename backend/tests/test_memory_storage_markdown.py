"""File/JSON + single-fact Markdown storage contract tests."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig
from deerflow.agents.memory.backends.deermem.deermem.core.paths import fact_file_path
from deerflow.agents.memory.backends.deermem.deermem.core.storage import (
    FileMemoryStorage,
    MemoryRevisionConflict,
    MemoryStorageCorruption,
    create_empty_memory,
)


@pytest.fixture
def storage(tmp_path: Path) -> FileMemoryStorage:
    return FileMemoryStorage(DeerMemConfig(storage_path=str(tmp_path)))


def _memory_with_fact(content: str = "Project uses Python 3.12") -> dict:
    memory = create_empty_memory()
    memory["facts"] = [
        {
            "id": "fact_01HZZZZZZZZZZZZZZZZZZZZZZZ",
            "content": content,
            "category": "constraint",
            "topics": ["python", "runtime"],
            "confidence": 0.95,
            "createdAt": "2026-07-17T00:00:00Z",
            "source": {"type": "manual", "threadId": "thread-1"},
            "revision": 1,
        }
    ]
    return memory


def test_agent_scope_uses_fact_directories_but_one_user_memory_file(storage: FileMemoryStorage, tmp_path: Path) -> None:
    assert storage.save(_memory_with_fact("A"), "agent-a", user_id="alice")
    assert storage.save(_memory_with_fact("B"), "agent-b", user_id="alice")

    assert storage.load("agent-a", user_id="alice")["facts"][0]["content"] == "A"
    assert storage.load("agent-b", user_id="alice")["facts"][0]["content"] == "B"
    assert (tmp_path / "users" / "alice" / "memory.json").exists()
    assert not (tmp_path / "users" / "alice" / "agents" / "agent-a" / "memory.json").exists()
    assert list((tmp_path / "users" / "alice" / "agents" / "agent-a" / "facts").glob("**/*.md"))
    assert list((tmp_path / "users" / "alice" / "agents" / "agent-b" / "facts").glob("**/*.md"))


def test_thread_id_is_source_only_not_storage_bucket(storage: FileMemoryStorage) -> None:
    fact = _memory_with_fact()["facts"][0]
    assert fact["source"]["threadId"] == "thread-1"
    assert storage.save(_memory_with_fact(), "agent-a", user_id="alice")
    memory_path = storage._get_memory_file_path("agent-a", user_id="alice")
    assert "thread-1" not in str(memory_path)


def test_memory_json_contains_only_global_summaries_and_agent_fact_is_markdown(storage: FileMemoryStorage) -> None:
    memory = _memory_with_fact()
    memory["user"]["workContext"] = {"summary": "global profile", "updatedAt": "now"}
    assert storage.save(memory, "agent-a", user_id="alice")
    memory_path = storage._get_memory_file_path("agent-a", user_id="alice")
    persisted = json.loads(memory_path.read_text(encoding="utf-8"))

    assert persisted["version"] == "2.0"
    assert "facts" not in persisted
    assert set(persisted) == {"version", "revision", "lastUpdated", "user", "history"}
    fact_path = fact_file_path(memory_path, "fact_01HZZZZZZZZZZZZZZZZZZZZZZZ", agent_name="agent-a")
    text = fact_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "user_id: alice" in text
    assert "agent_name: agent-a" in text
    assert "# Project uses Python 3.12" in text


def test_load_keeps_frontend_shape_but_only_agent_load_returns_facts(storage: FileMemoryStorage) -> None:
    assert storage.save(_memory_with_fact(), "agent-a", user_id="alice")

    global_memory = storage.load(user_id="alice")
    agent_memory = storage.load("agent-a", user_id="alice")

    assert global_memory["facts"] == []
    assert agent_memory["facts"][0]["topics"] == ["python", "runtime"]
    assert agent_memory["facts"][0]["scope"] == {"userId": "alice", "agentName": "agent-a"}


def test_agent_save_does_not_overwrite_global_summaries(storage: FileMemoryStorage) -> None:
    global_memory = create_empty_memory()
    global_memory["user"]["workContext"] = {"summary": "works remotely", "updatedAt": "global"}
    assert storage.save(global_memory, user_id="alice")

    agent_memory = storage.load("agent-a", user_id="alice")
    agent_memory["user"]["workContext"] = {"summary": "project secret", "updatedAt": "agent"}
    agent_memory["facts"] = _memory_with_fact()["facts"]
    assert storage.save(agent_memory, "agent-a", user_id="alice", expected_revision=1)

    assert storage.load(user_id="alice")["user"]["workContext"]["summary"] == "works remotely"


def test_removed_fact_is_physically_deleted(storage: FileMemoryStorage) -> None:
    assert storage.save(_memory_with_fact(), "agent-a", user_id="alice")
    memory_path = storage._get_memory_file_path("agent-a", user_id="alice")
    fact_path = fact_file_path(memory_path, "fact_01HZZZZZZZZZZZZZZZZZZZZZZZ", agent_name="agent-a")
    assert fact_path.exists()

    empty = create_empty_memory()
    assert storage.save(empty, "agent-a", user_id="alice")
    assert not fact_path.exists()


def test_cached_document_is_not_mutable_by_caller(storage: FileMemoryStorage) -> None:
    assert storage.save(_memory_with_fact(), "agent-a", user_id="alice")
    first = storage.load("agent-a", user_id="alice")
    first["facts"][0]["content"] = "mutated outside storage"
    second = storage.load("agent-a", user_id="alice")
    assert second["facts"][0]["content"] == "Project uses Python 3.12"


def test_corrupt_manifest_raises_and_is_not_treated_as_empty(storage: FileMemoryStorage) -> None:
    path = storage._get_memory_file_path(user_id="alice")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(MemoryStorageCorruption):
        storage.load(user_id="alice")
    assert path.read_text(encoding="utf-8") == "{broken"


def test_manifest_revision_conflict_rejects_stale_write(storage: FileMemoryStorage) -> None:
    assert storage.save(_memory_with_fact(), "agent-a", user_id="alice")
    current = storage.load("agent-a", user_id="alice")
    assert current["revision"] == 1
    assert storage.save(_memory_with_fact("new"), "agent-a", user_id="alice", expected_revision=1)
    with pytest.raises(MemoryRevisionConflict):
        storage.save(_memory_with_fact("stale"), "agent-a", user_id="alice", expected_revision=1)


def test_storage_delegates_index_lifecycle_and_search_to_retrieval(tmp_path: Path) -> None:
    class FakeRetrieval:
        def __init__(self) -> None:
            self.upserts = []
            self.removes = []

        def upsert(self, fact, *, scope, path):
            self.upserts.append((fact["id"], scope, path))

        def remove(self, fact_id, *, scope):
            self.removes.append((fact_id, scope))

        def search(self, query, *, scopes, top_k, mode, filters):
            return [{"id": "fact-result", "score": 0.9, "query": query}]

    retrieval = FakeRetrieval()
    scoped = FileMemoryStorage(DeerMemConfig(storage_path=str(tmp_path)), retrieval=retrieval)
    assert scoped.save(_memory_with_fact(), "agent-a", user_id="alice")
    assert retrieval.upserts[0][1] == {"userId": "alice", "agentName": "agent-a"}
    assert scoped.search_facts("python", scopes=[{"userId": "alice", "agentName": "agent-a"}])[0]["score"] == 0.9

    assert scoped.save(create_empty_memory(), "agent-a", user_id="alice")
    assert retrieval.removes == [("fact_01HZZZZZZZZZZZZZZZZZZZZZZZ", {"userId": "alice", "agentName": "agent-a"})]


def test_prepared_journal_restores_previous_manifest_and_fact(storage: FileMemoryStorage) -> None:
    assert storage.save(_memory_with_fact("original"), "agent-a", user_id="alice")
    memory_path = storage._get_memory_file_path("agent-a", user_id="alice")
    memory = json.loads(memory_path.read_text(encoding="utf-8"))
    fact_id = "fact_01HZZZZZZZZZZZZZZZZZZZZZZZ"
    fact_path = fact_file_path(memory_path, fact_id, agent_name="agent-a")
    operation_id = "op-recovery-test"
    recovery = memory_path.parent / ".recovery" / operation_id
    recovery.mkdir(parents=True)
    shutil.copy2(memory_path, recovery / "memory.json")
    shutil.copy2(fact_path, recovery / f"{fact_id}.md")
    relative_fact_path = fact_path.relative_to(memory_path.parent).as_posix()
    journal = {
        "operationId": operation_id,
        "state": "prepared",
        "agentName": "agent-a",
        "expectedRevision": memory["revision"],
        "nextRevision": memory["revision"] + 1,
        "factIds": [fact_id],
        "oldEntries": {fact_id: {"path": relative_fact_path}},
    }
    (memory_path.parent / ".memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    fact_path.write_text("corrupt in-progress content", encoding="utf-8")

    loaded = storage.load("agent-a", user_id="alice")

    assert loaded["facts"][0]["content"] == "original"
    assert not (memory_path.parent / ".memory.journal.json").exists()


def test_fact_repository_applies_upsert_and_physical_delete(storage: FileMemoryStorage) -> None:
    first = storage.upsert_fact(_memory_with_fact()["facts"][0], user_id="alice", agent_name="agent-a", expected_revision=0)
    assert first["revision"] == 1
    assert storage.get_fact("fact_01HZZZZZZZZZZZZZZZZZZZZZZZ", user_id="alice", agent_name="agent-a")["content"] == "Project uses Python 3.12"

    updated = copy.deepcopy(first["facts"][0])
    updated["content"] = "Project uses Python 3.13"
    second = storage.apply_changes(
        {"upserts": [updated]},
        user_id="alice",
        agent_name="agent-a",
        expected_manifest_revision=1,
    )
    assert second["facts"][0]["content"] == "Project uses Python 3.13"

    memory_path = storage._get_memory_file_path("agent-a", user_id="alice")
    fact_path = fact_file_path(memory_path, updated["id"], agent_name="agent-a")
    assert fact_path.exists()
    third = storage.delete_fact(updated["id"], user_id="alice", agent_name="agent-a", expected_revision=2)
    assert third["facts"] == []
    assert not fact_path.exists()


def test_search_facts_declares_and_uses_substring_fallback(storage: FileMemoryStorage) -> None:
    storage.upsert_fact(_memory_with_fact()["facts"][0], user_id="alice", agent_name="agent-a", expected_revision=0)

    results = storage.search_facts(
        "python",
        scopes=[{"userId": "alice", "agentName": "agent-a"}],
    )

    assert results[0]["fact"]["content"] == "Project uses Python 3.12"
    assert results[0]["matchType"] == "substring"
    assert storage.retrieval_status()["mode"] == "substring_fallback"
    assert "substring-fallback" in storage.capabilities()


def test_strict_scope_and_custom_manifest_filename(tmp_path: Path) -> None:
    strict = FileMemoryStorage(DeerMemConfig(storage_path=str(tmp_path), strict_user_scope=True, manifest_filename="index.json"))
    with pytest.raises(ValueError, match="user_id"):
        strict.load()

    strict.upsert_fact(_memory_with_fact()["facts"][0], user_id="alice", agent_name="agent-a", expected_revision=0)
    assert (tmp_path / "users" / "alice" / "index.json").exists()


def test_explicit_migrate_converts_legacy_json(storage: FileMemoryStorage) -> None:
    path = storage._get_memory_file_path(user_id="alice")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_memory_with_fact()), encoding="utf-8")

    report = storage.migrate(user_id="alice", agent_name="agent-a")

    assert report["migrated"] is True
    assert report["fromVersion"] == "1.0"
    assert report["toVersion"] == "2.0"
    assert "facts" not in json.loads(path.read_text(encoding="utf-8"))
    assert fact_file_path(path, "fact_01HZZZZZZZZZZZZZZZZZZZZZZZ", agent_name="agent-a").exists()


def test_first_agent_load_removes_legacy_per_agent_memory_json(storage: FileMemoryStorage) -> None:
    user_memory_path = storage._get_memory_file_path("agent-a", user_id="alice")
    legacy_path = user_memory_path.parent / "agents" / "agent-a" / "memory.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps(_memory_with_fact("legacy agent fact")), encoding="utf-8")

    loaded = storage.load("agent-a", user_id="alice")

    assert loaded["facts"][0]["content"] == "legacy agent fact"
    assert not legacy_path.exists()
    assert user_memory_path.exists()
    assert "facts" not in json.loads(user_memory_path.read_text(encoding="utf-8"))


def test_fact_repository_requires_agent_name(storage: FileMemoryStorage) -> None:
    with pytest.raises(ValueError, match="agent_name"):
        storage.upsert_fact(_memory_with_fact()["facts"][0], user_id="alice", expected_revision=0)
