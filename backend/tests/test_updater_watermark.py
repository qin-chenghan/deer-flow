"""Tests for the in-memory watermark (skip already-extracted messages)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig
from deerflow.agents.memory.backends.deermem.deermem.core.storage import MemoryStorage
from deerflow.agents.memory.backends.deermem.deermem.core.updater import MemoryUpdater


class _FakeLLM:
    """Returns a canned empty-update response; counts invocations."""

    def __init__(self) -> None:
        self.invoke_count = 0
        self._response = _EmptyResponse()

    def invoke(self, prompt: Any, config: Any = None) -> Any:
        self.invoke_count += 1
        return self._response


class _EmptyResponse:
    content = json.dumps(
        {
            "user": {},
            "history": {},
            "newFacts": [],
            "factsToRemove": [],
            "staleFactsToRemove": [],
            "staleFactsToExtend": [],
            "factsToConsolidate": [],
        }
    )
    usage_metadata: dict[str, int] | None = None


class _FakeStorage(MemoryStorage):
    """Minimal in-memory storage stub (load/save) for the save() path."""

    def __init__(self) -> None:
        self.memory: dict[str, Any] = {"version": "2.0", "revision": 0, "user": {}, "history": {}, "facts": []}

    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        return json.loads(json.dumps(self.memory))

    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        return self.load(agent_name, user_id=user_id)

    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None, expected_revision: int | None = None) -> bool:
        self.memory = json.loads(json.dumps(memory_data))
        return True


def _config(**overrides: Any) -> DeerMemConfig:
    base: dict[str, Any] = {}
    base.update(overrides)
    return DeerMemConfig(**base)


def _msgs(*texts: str) -> list[Any]:
    out: list[Any] = []
    for t in texts:
        out.append(HumanMessage(content=t))
        out.append(AIMessage(content=f"reply-{t}"))
    return out


def test_watermark_skips_already_extracted_messages() -> None:
    llm = _FakeLLM()
    updater = MemoryUpdater(_config(), _FakeStorage(), llm)
    messages = _msgs("first")

    updater.update_memory(messages, thread_id="t1", agent_name="a", user_id="u")
    assert llm.invoke_count == 1

    # Same messages again -> nothing new since the watermark -> skipped, no LLM call.
    result = updater.update_memory(messages, thread_id="t1", agent_name="a", user_id="u")
    assert result is True
    assert llm.invoke_count == 1


def test_watermark_feeds_only_new_messages_on_growth() -> None:
    llm = _FakeLLM()
    updater = MemoryUpdater(_config(), _FakeStorage(), llm)
    messages = _msgs("first")
    updater.update_memory(messages, thread_id="t1", agent_name="a", user_id="u")
    assert llm.invoke_count == 1

    # Append a new turn; only the new turn is fed (watermark = prior length).
    messages += _msgs("second")
    updater.update_memory(messages, thread_id="t1", agent_name="a", user_id="u")
    assert llm.invoke_count == 2


def test_watermark_is_per_thread() -> None:
    llm = _FakeLLM()
    updater = MemoryUpdater(_config(), _FakeStorage(), llm)
    messages = _msgs("first")
    # Thread t1 extracts; thread t2 has its own watermark (starts at 0).
    updater.update_memory(messages, thread_id="t1", agent_name="a", user_id="u")
    updater.update_memory(messages, thread_id="t2", agent_name="a", user_id="u")
    assert llm.invoke_count == 2


def test_watermark_resets_when_conversation_shrinks() -> None:
    llm = _FakeLLM()
    updater = MemoryUpdater(_config(), _FakeStorage(), llm)
    long_msgs = _msgs("first", "second", "third")
    updater.update_memory(long_msgs, thread_id="t1", agent_name="a", user_id="u")
    assert llm.invoke_count == 1
    # A shorter message list (e.g. after summarization) must not get stuck at a
    # watermark past the end; it re-extracts from the start.
    short_msgs = _msgs("only")
    updater.update_memory(short_msgs, thread_id="t1", agent_name="a", user_id="u")
    assert llm.invoke_count == 2
