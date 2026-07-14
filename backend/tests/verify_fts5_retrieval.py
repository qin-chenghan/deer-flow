#!/usr/bin/env python3
"""FTS5 检索能力验证脚本

验证项：
1. 基础 BM25 检索
2. jieba 中文分词
3. 高级查询语法（AND/OR/NOT/短语/前缀）
4. 保底策略（语法错误降级）
5. category 过滤
6. 时间衰减排序
7. scope 隔离（user_id）
8. 索引同步（写入后可检索、删除后消失）
9. FTS5 vs 子串匹配对比
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 确保 backend 包在 path 中
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir / "packages" / "harness"))
sys.path.insert(0, str(backend_dir))

os.environ.setdefault("DEERFLOW_CONFIG_FILE", str(backend_dir.parent / "config.example.yaml"))

from deerflow.agents.memory.backends.deermem.deer_mem import DeerMem  # noqa: E402
from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig  # noqa: E402
from deerflow.agents.memory.backends.deermem.deermem.core.retrieval import _jieba_available  # noqa: E402


def section(title: str):
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def print_results(results: list[dict], title: str = ""):
    if title:
        print(f"\n  ▶ {title}")
    if not results:
        print("    (无结果)")
        return
    for i, r in enumerate(results, 1):
        score = r.get("score", r.get("confidence", 0))
        content = r.get("content", "")[:70]
        category = r.get("category", "?")
        print(f"    {i}. [{score:.3f}] ({category}) {content}")


def main():
    print("=" * 72)
    print("  FTS5 检索能力验证")
    print(f"  jieba: {'✓ 可用' if _jieba_available else '✗ 未安装'}")
    print("=" * 72)

    # ── 创建临时 DeerMem 实例 ──────────────────────────────────────────

    tmpdir = tempfile.mkdtemp(prefix="deermem-test-")
    config = DeerMemConfig.from_backend_config(
        {
            "storage_path": tmpdir,
            "token_counting": "char",  # 避免 tiktoken 网络下载
        }
    )

    # 不需要 LLM（跳过 memory update，直接用 create_fact）
    manager = DeerMem.__new__(DeerMem)
    manager._config = config
    from deerflow.agents.memory.backends.deermem.deermem.core.storage import create_storage

    manager._storage = create_storage(config)
    manager._llm = None
    from deerflow.agents.memory.backends.deermem.deermem.core.updater import MemoryUpdater

    manager._updater = MemoryUpdater(config, manager._storage, None)
    manager._queue = None
    manager._retrieval = None
    manager._retrieval_dirty = True

    user = "test_user"
    agent = None

    # ── 准备测试数据 ───────────────────────────────────────────────────

    facts_data = [
        ("Prefers FTS5 over Python keyword matching for search", "preference", 0.95),
        ("BM25 returns negative scores in FTS5, closer to zero is more relevant", "knowledge", 0.85),
        ("Use DeerMem not DefaultMemoryManager as the default implementation name", "correction", 0.98),
        ("记忆系统使用 SQLite FTS5 全文搜索引擎", "knowledge", 0.9),
        ("用户偏好使用中文交流，技术栈包括 Python 和 TypeScript", "preference", 0.92),
        ("实现了基于 BM25 算法的相关性排序，支持时间衰减加权", "knowledge", 0.88),
        ("Deployment uses Docker Compose with 3 services", "context", 0.7),
        ("Kubernetes kubelet performance optimization for large clusters", "knowledge", 0.8),
        ("Don't use pip, use uv for package management", "correction", 0.99),
        ("Memory injection budget is 2000 tokens with tiktoken counting", "context", 0.75),
    ]

    for content, category, confidence in facts_data:
        manager.create_fact(content, category=category, confidence=confidence, user_id=user)

    print(f"\n  ✓ 已插入 {len(facts_data)} 条测试数据")

    # ── 1. 基础 BM25 检索 ─────────────────────────────────────────────

    section("1. 基础 BM25 检索")

    results = manager.search("FTS5 search", top_k=5, user_id=user)
    print_results(results, 'search("FTS5 search")')

    results = manager.search("BM25", top_k=5, user_id=user)
    print_results(results, 'search("BM25")')

    results = manager.search("Docker", top_k=3, user_id=user)
    print_results(results, 'search("Docker")')

    # ── 2. jieba 中文分词 ─────────────────────────────────────────────

    section("2. jieba 中文分词")

    results = manager.search("检索引擎", top_k=5, user_id=user)
    print_results(results, 'search("检索引擎")')

    results = manager.search("排序", top_k=5, user_id=user)
    print_results(results, 'search("排序")')

    results = manager.search("中文交流", top_k=5, user_id=user)
    print_results(results, 'search("中文交流")')

    # ── 3. 高级查询语法 ───────────────────────────────────────────────

    section("3. 高级查询语法")

    results = manager.search("FTS5 AND BM25", top_k=5, user_id=user)
    print_results(results, "AND 交集")

    results = manager.search("FTS5 OR Kubernetes", top_k=5, user_id=user)
    print_results(results, "OR 并集")

    results = manager.search("memory NOT FTS5", top_k=5, user_id=user)
    print_results(results, "NOT 排除")

    results = manager.search('"FTS5"', top_k=5, user_id=user)
    print_results(results, "短语搜索")

    results = manager.search("kubern*", top_k=3, user_id=user)
    print_results(results, "前缀匹配")

    # ── 4. 保底策略 ───────────────────────────────────────────────────

    section("4. 保底策略（语法错误降级）")

    results = manager.search("memory (broken {", top_k=5, user_id=user)
    print_results(results, "语法错误自动降级")

    # ── 5. category 过滤 ──────────────────────────────────────────────

    section("5. category 过滤")

    results = manager.search("memory", top_k=10, user_id=user, category="preference")
    print_results(results, "category=preference")

    results = manager.search("memory", top_k=10, user_id=user, category="correction")
    print_results(results, "category=correction")

    results = manager.search("memory", top_k=10, user_id=user, category="knowledge")
    print_results(results, "category=knowledge")

    # ── 6. 时间衰减 ───────────────────────────────────────────────────

    section("6. 时间衰减排序")

    # 插入一条 60 天前的旧数据
    old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat().replace("+00:00", "Z")
    manager.create_fact(
        "FTS5 memory architecture design initial discussion",
        category="knowledge",
        confidence=0.9,
        user_id=user,
    )
    # 手动改 createdAt（通过直接操作 storage）
    memory_data = manager.get_memory(user_id=user)
    facts = memory_data.get("facts", [])
    old_fact = facts[-1]
    old_fact["createdAt"] = old_date
    manager._updater._storage.save(memory_data, agent_name=None, user_id=user)
    manager._retrieval_dirty = True

    results = manager.search("FTS5 memory architecture", top_k=5, user_id=user)
    print_results(results, "60天前的旧数据应排后面")

    print("\n  ▶ 带时间戳查看：")
    for r in results:
        created = r.get("createdAt", "?")[:10]
        print(f"    [{r.get('score', 0):.3f}] {created}  {r.get('content', '')[:50]}")

    # ── 7. scope 隔离 ─────────────────────────────────────────────────

    section("7. scope 隔离（user_id）")

    results = manager.search("FTS5", top_k=5, user_id="other_user")
    print_results(results, "user_id=other_user 搜 FTS5 -> 应无结果")

    results = manager.search("FTS5", top_k=5, user_id=user)
    print_results(results, f"user_id={user} 搜 FTS5 -> 有结果")

    # ── 8. 索引同步 ───────────────────────────────────────────────────

    section("8. 索引同步")

    # 插入新 fact
    manager.create_fact(
        "New fact about vector embedding retrieval",
        category="knowledge",
        confidence=0.85,
        user_id=user,
    )
    results = manager.search("vector embedding", top_k=3, user_id=user)
    print_results(results, "插入新 fact 后立即可检索")

    # 删除 fact
    fact_id = results[0]["id"] if results else None
    if fact_id:
        manager.delete_fact(fact_id, user_id=user)
        results = manager.search("vector embedding", top_k=3, user_id=user)
        print_results(results, f"删除 fact({fact_id[:12]}...) 后不再返回")

    # clear
    manager.clear_memory(user_id=user)
    results = manager.search("FTS5", top_k=5, user_id=user)
    print_results(results, "clear_memory 后全空")

    # ── 9. FTS5 vs 子串匹配对比 ───────────────────────────────────────

    section("9. FTS5 vs 子串匹配对比")

    # 重新插入数据
    for content, category, confidence in facts_data:
        manager.create_fact(content, category=category, confidence=confidence, user_id=user)

    query = "FTS5 ranking"
    print(f"\n  query: '{query}'")

    # FTS5
    fts5_results = manager._fts5_search(query, top_k=5, user_id=user, agent_name=agent, category=None)
    print(f"\n  ▶ FTS5 BM25 检索 ({len(fts5_results)} 条):")
    for r in fts5_results:
        print(f"    [{r.get('score', 0):.3f}] {r.get('content', '')[:60]}")

    # 子串
    sub_results = manager._substring_search(query, top_k=5, user_id=user, agent_name=agent, category=None)
    print(f"\n  ▶ 子串匹配 ({len(sub_results)} 条):")
    for r in sub_results:
        print(f"    [conf={r.get('confidence', 0):.2f}] {r.get('content', '')[:60]}")

    if fts5_results and not sub_results:
        print("\n  ✓ FTS5 能搜到子串匹配搜不到的结果（BM25 部分匹配优势）")
    elif len(fts5_results) > len(sub_results):
        print("\n  ✓ FTS5 返回更多相关结果")
    else:
        print("\n  · 两者结果相近")

    # ── 清理 ──────────────────────────────────────────────────────────

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    print("=" * 72)
    print("  验证完成 ✓")
    print("=" * 72)


if __name__ == "__main__":
    main()
