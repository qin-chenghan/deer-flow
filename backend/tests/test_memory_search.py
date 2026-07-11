"""Tests for DeerMem.search (the ABC search implementation).

DeerMem.search is a case-insensitive substring search over stored facts
(stand-in for the planned semantic retrieval). Category filtering is not on
the ABC ``search`` signature; it lives in the ``memory_search`` tool
(see test_memory_tools.py). These tests cover the backend's own search.
"""

from types import SimpleNamespace

from deerflow.agents.memory.backends.deermem.deer_mem import DeerMem


def _make_fact(content: str, category: str = "context", confidence: float = 0.7) -> dict:
    return {
        "id": f"fact_test_{hash(content) & 0xFFFFFFFF:08x}",
        "content": content,
        "category": category,
        "confidence": confidence,
        "createdAt": "2026-07-09T00:00:00Z",
        "source": "test",
    }


def _deer_mem_with_facts(facts: list[dict]) -> DeerMem:
    """Build a DeerMem whose updater returns the given facts (no disk I/O)."""
    mgr = DeerMem(backend_config=None)
    mgr._updater = SimpleNamespace(
        get_memory_data=lambda agent_name=None, *, user_id=None: {"facts": facts}
    )
    return mgr


class TestDeerMemSearch:
    """Tests for DeerMem.search."""

    def test_basic_substring_match(self):
        """Should find facts containing the query string (case-insensitive)."""
        facts = [
            _make_fact("User prefers Python", "preference", 0.9),
            _make_fact("User works with TypeScript", "context", 0.7),
            _make_fact("User lives in Beijing", "personal", 0.8),
        ]
        mgr = _deer_mem_with_facts(facts)

        results = mgr.search("python")
        assert len(results) == 1
        assert results[0]["content"] == "User prefers Python"

    def test_case_insensitive(self):
        """Should match regardless of case."""
        mgr = _deer_mem_with_facts([_make_fact("User prefers Python", "preference", 0.9)])

        assert len(mgr.search("PYTHON")) == 1
        assert len(mgr.search("python")) == 1
        assert len(mgr.search("Python")) == 1

    def test_empty_query_returns_empty(self):
        """Should return empty list for empty query, not error."""
        mgr = _deer_mem_with_facts([_make_fact("Some fact")])

        assert mgr.search("") == []
        assert mgr.search("   ") == []

    def test_no_match_returns_empty(self):
        """Should return empty list when nothing matches."""
        mgr = _deer_mem_with_facts([_make_fact("User prefers Python")])

        assert mgr.search("Rust") == []

    def test_sorted_by_confidence_desc(self):
        """Should return results sorted by confidence descending."""
        facts = [
            _make_fact("Fact A", confidence=0.3),
            _make_fact("Fact B", confidence=0.9),
            _make_fact("Fact C", confidence=0.6),
        ]
        mgr = _deer_mem_with_facts(facts)

        results = mgr.search("Fact")
        assert len(results) == 3
        assert results[0]["confidence"] == 0.9
        assert results[1]["confidence"] == 0.6
        assert results[2]["confidence"] == 0.3

    def test_respects_top_k(self):
        """Should return at most ``top_k`` results."""
        facts = [_make_fact(f"Fact {i}", confidence=0.5) for i in range(20)]
        mgr = _deer_mem_with_facts(facts)

        results = mgr.search("Fact", top_k=5)
        assert len(results) == 5

    def test_non_positive_top_k_returns_empty(self):
        """Should return empty for top_k <= 0 (no negative-slice expansion)."""
        mgr = _deer_mem_with_facts([_make_fact(f"Fact {i}", confidence=0.5) for i in range(3)])

        assert mgr.search("Fact", top_k=0) == []
        assert mgr.search("Fact", top_k=-1) == []

    def test_no_facts_returns_empty(self):
        """Should return empty list when memory has no facts."""
        mgr = _deer_mem_with_facts([])

        assert mgr.search("anything") == []

    def test_non_string_content_is_skipped(self):
        """Facts whose content is not a str are skipped, not crashed on."""
        facts = [
            {"id": "f1", "content": "likes uv", "category": "preference", "confidence": 0.9},
            {"id": "f2", "content": 42, "category": "context", "confidence": 0.5},
            {"id": "f3", "content": None, "category": "context", "confidence": 0.5},
        ]
        mgr = _deer_mem_with_facts(facts)

        results = mgr.search("uv")
        assert len(results) == 1
        assert results[0]["id"] == "f1"
