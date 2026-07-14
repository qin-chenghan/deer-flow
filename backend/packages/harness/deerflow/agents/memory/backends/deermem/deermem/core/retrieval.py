"""FTS5-based retrieval engine for DeerMem.

Provides BM25 full-text search over stored facts with:
- jieba Chinese tokenization (optional, falls back to whitespace)
- FTS5 MATCH syntax support (AND/OR/NOT/phrase/prefix) with fallback
- Time-decay + confidence-weighted ranking
- Category filtering
- Scope (user_id) isolation

This module is internal to DeerMem -- not on the MemoryManager ABC.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── jieba (optional) ──────────────────────────────────────────────────

try:
    import jieba

    _jieba_available = True
except ImportError:
    _jieba_available = False


def _tokenize(text: str) -> list[str]:
    """Tokenize text: jieba for Chinese, whitespace split for English."""
    if not text or not text.strip():
        return []
    if _jieba_available:
        return [t for t in jieba.cut(text) if t.strip()]
    return [t for t in text.split() if t.strip()]


# ── FTS5 query preprocessing ──────────────────────────────────────────

_FTS5_ADVANCED_RE = re.compile(
    r"(\bAND\b|\bOR\b|\bNOT\b|\bNEAR\b"
    r'|"\w.*?"'  # phrase "..."
    r"|\+\w"  # required +word
    r"|-\w"  # excluded -word
    r"|\w+\*"  # prefix prefix*
    r"|\(.*?\))"  # group (...)
)


def _is_advanced_query(query: str) -> bool:
    """Detect whether the query uses FTS5 advanced syntax."""
    return bool(_FTS5_ADVANCED_RE.search(query))


def _build_fallback_query(query: str) -> str:
    """Convert natural-language query to FTS5 OR query (fallback strategy)."""
    tokens = _tokenize(query)
    if not tokens:
        return ""
    return " OR ".join(tokens)


# ── Core retrieval engine ─────────────────────────────────────────────


class FTS5Retrieval:
    """SQLite FTS5-based retrieval engine.

    Query strategy:
      1. Advanced FTS5 syntax -> pass through to MATCH
      2. Natural language -> jieba tokenize + OR join
      3. Syntax error -> fall back to tokenized OR query
      4. Still fails -> return empty

    Ranking:
      BM25 score × time_decay + confidence × 0.2
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        conn = self._conn
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                doc_id UNINDEXED,
                content,
                category UNINDEXED,
                scope_user UNINDEXED,
                scope_agent UNINDEXED,
                created_at UNINDEXED,
                confidence UNINDEXED,
                tokenize='unicode61'
            )
            """
        )
        conn.commit()

    # ── Index operations ───────────────────────────────────────────────

    def _preprocess_content(self, content: str) -> str:
        """Preprocess content for indexing: jieba tokenize for Chinese."""
        if not content:
            return ""
        if _jieba_available:
            tokens = _tokenize(content)
            return " ".join(tokens)
        return content

    def index_fact(
        self,
        fact_id: str,
        content: str,
        category: str = "context",
        confidence: float = 0.5,
        created_at: str | None = None,
        scope_user: str | None = None,
        scope_agent: str | None = None,
    ) -> None:
        """Insert or update a fact in the FTS5 index."""
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        indexed_content = self._preprocess_content(content)

        conn = self._conn
        # Delete existing entry with same doc_id (INSERT OR REPLACE for FTS5)
        conn.execute("DELETE FROM memory_fts WHERE doc_id = ?", (fact_id,))
        conn.execute(
            """
            INSERT INTO memory_fts(doc_id, content, category, scope_user, scope_agent, created_at, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fact_id, indexed_content, category, scope_user or "", scope_agent or "", created_at or now, confidence),
        )
        conn.commit()

    def remove_fact(self, fact_id: str) -> None:
        """Remove a fact from the FTS5 index."""
        conn = self._conn
        conn.execute("DELETE FROM memory_fts WHERE doc_id = ?", (fact_id,))
        conn.commit()

    def clear_index(self) -> None:
        """Clear the entire FTS5 index."""
        conn = self._conn
        conn.execute("DELETE FROM memory_fts")
        conn.commit()

    def rebuild_from_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        scope_user: str | None = None,
        scope_agent: str | None = None,
    ) -> None:
        """Rebuild the entire index from a list of fact dicts."""
        self.clear_index()
        for fact in facts:
            fact_id = fact.get("id", "")
            content = fact.get("content", "")
            if not fact_id or not content:
                continue
            self.index_fact(
                fact_id,
                content,
                category=fact.get("category", "context"),
                confidence=fact.get("confidence", 0.5),
                created_at=fact.get("createdAt"),
                scope_user=scope_user,
                scope_agent=scope_agent,
            )

    # ── Search ─────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        scope_user: str | None = None,
        scope_agent: str | None = None,
        category: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """FTS5 BM25 search with category and scope filtering.

        Returns list of fact dicts (id, content, category, confidence,
        createdAt, source, score, bm25_score) sorted by relevance.
        """
        if not query or not query.strip() or top_k <= 0:
            return []

        query = query.strip()

        # Determine query strategy
        if _is_advanced_query(query):
            fts5_query = query
            strategy = "advanced"
        else:
            fts5_query = _build_fallback_query(query)
            strategy = "tokenized"

        # Try search
        results = self._execute_search(fts5_query, scope_user, scope_agent, category, top_k)
        if results is not None:
            return results

        # Fallback: advanced syntax error -> tokenized OR
        if strategy == "advanced":
            fallback = _build_fallback_query(query)
            if fallback and fallback != fts5_query:
                results = self._execute_search(fallback, scope_user, scope_agent, category, top_k)
                if results is not None:
                    return results

        return []

    def _execute_search(
        self,
        fts5_query: str,
        scope_user: str | None,
        scope_agent: str | None,
        category: str | None,
        top_k: int,
    ) -> list[dict[str, Any]] | None:
        """Execute FTS5 query. Return None on syntax error."""
        if not fts5_query:
            return []

        conditions = ["memory_fts MATCH ?"]
        params: list[Any] = [fts5_query]

        if scope_user:
            conditions.append("scope_user = ?")
            params.append(scope_user)
        if scope_agent:
            conditions.append("scope_agent = ?")
            params.append(scope_agent)
        if category:
            conditions.append("category = ?")
            params.append(category)

        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT doc_id, content, category, scope_user, scope_agent,
                   created_at, confidence,
                   bm25(memory_fts, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0) AS bm25_score
            FROM memory_fts
            WHERE {where_clause}
            ORDER BY bm25_score
            LIMIT ?
        """
        params.append(top_k * 2)

        try:
            conn = self._conn
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            logger.debug("FTS5 query syntax error: %s (query: %s)", e, fts5_query)
            return None

        results: list[dict[str, Any]] = []
        for row in rows:
            (
                doc_id,
                content,
                cat,
                s_user,
                s_agent,
                created_at,
                confidence,
                bm25_score,
            ) = row

            score = self._compute_final_score(
                bm25_score=-bm25_score,  # FTS5 returns negative
                confidence=confidence,
                created_at=created_at,
            )

            results.append(
                {
                    "id": doc_id,
                    "content": content,
                    "category": cat,
                    "confidence": confidence,
                    "createdAt": created_at,
                    "source": "fts5",
                    "score": score,
                    "bm25_score": -bm25_score,
                }
            )

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]

    def _compute_final_score(
        self,
        bm25_score: float,
        confidence: float,
        created_at: str,
    ) -> float:
        """Combined score: BM25 × time_decay + confidence weight."""
        score = bm25_score * 1.0

        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_days = (datetime.now(UTC) - dt).days
            time_decay = 1.0 if age_days < 30 else math.exp(-0.01 * (age_days - 30))
            score *= time_decay
        except (ValueError, TypeError):
            pass

        score += confidence * 0.2

        return score

    # ── Stats ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Index statistics."""
        conn = self._conn
        total = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        return {
            "total_docs": total,
            "jieba": _jieba_available,
            "db_path": self._db_path,
        }

    def close(self) -> None:
        self._conn.close()
