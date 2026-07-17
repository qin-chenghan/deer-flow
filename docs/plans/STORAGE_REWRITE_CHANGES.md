# DeerMem Storage 改写实施说明（storage-only）

> 基线：DeerFlow 官方 `upstream/main`，提交 `bc6f1adc`  
> 实施范围：File/JSON + 单 fact Markdown、repository、并发和恢复、检索适配接口。  
> 明确排除：`project_id` scope、MemoryManager ABC/runtime scope 改造、SQLite、检索算法。

## 1. Scope 边界

本轮保持现有长期记忆隔离键：

```text
MemoryScope = (user_id, agent_name)
```

`thread_id` 只保存在 `source.threadId`，不参与目录分桶。Project scope 变更面涉及 Manager、Gateway、runtime、middleware、tool、subagent、retrieval 和 injection，已撤回并留待独立 PR。

## 2. 存储结构

```text
{storage_root}/users/{user_id}/
├── memory.json
├── facts/{id前两位}/{fact_id}.md
└── agents/{agent_name}/
    ├── memory.json
    └── facts/{id前两位}/{fact_id}.md
```

- 单 fact Markdown 是 canonical storage。
- YAML front matter 保存 ID、schemaVersion、类别、topics、confidence、status、user/agent scope、source、时间、revision 和 consolidation 来源。
- Markdown 一级标题保存人类可读标题，正文保存原子事实。
- `memory.json` 只保存 user/history summaries、manifest revision，以及 fact 的路径、revision 和 SHA-256 hash。

## 3. 修改位置与功能

### Storage 核心

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/storage.py`
  - 文档版本升级为 `2.0`，旧整文档 `load/save` 继续作为兼容层。
  - 实现 Markdown renderer/parser、YAML front matter、JSON manifest 和内容 hash 校验。
  - 实现 `get_fact`、`list_facts`、`upsert_fact`、`delete_fact`、`apply_changes`、`get_summaries`、`update_summaries`。
  - fact 删除为物理删除。
  - 增加 manifest/fact revision 和 `expected_revision` 冲突检测。
  - 增加同 scope 线程锁、跨进程 advisory file lock、prepared/committed journal 和 recovery 快照。
  - 损坏的 manifest、Markdown、ID 或 hash 抛出明确异常，不再当空记忆覆盖。
  - 缓存按 `(user_id, agent_name)` 隔离，返回深拷贝，并以纳秒 mtime + 文件大小校验失效。
  - 旧 JSON facts 数组在首次访问或显式 `migrate()` 时幂等迁移。
  - 暴露 retrieval upsert/remove/search/rebuild/status/capabilities 接口；未配置 adapter 时声明并使用 substring fallback。
  - storage/retrieval dotted path 加载失败时 fail-fast。

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/paths.py`
  - 保持 user/agent 分桶。
  - 增加 fact ID 校验和 `facts/{prefix}/{fact_id}.md` 路径。
  - 支持安全的自定义 `manifest_filename` 和 `strict_user_scope`。

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/config.py`
- `config.example.yaml`
  - 新增 `strict_user_scope`、`fact_format`、`manifest_filename`、`file_lock_timeout_seconds`、`journal_enabled`、`retrieval_adapter` 配置。
  - 保留官方新版已有配置，不回退 staleness、shutdown flush 等上游字段。

### Updater、搜索和 API schema

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/updater.py`
  - File storage 写入携带当前 manifest revision，拒绝静默 lost update。
  - LLM updater 将前后状态计算为 upsert/delete/summaries change set，并调用 `apply_changes`。
  - 第三方旧 storage 未覆盖 repository 方法时继续使用兼容 `save`。

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deer_mem.py`
  - `search` 优先委托 storage/retrieval adapter，未命中时保持旧 substring 行为。

- `backend/app/gateway/routers/memory.py`
  - Fact response schema 可表达 structured source、schemaVersion、topics、status、user/agent scope、revision 和 consolidation 字段。
  - 未引入 project 参数，也未直接依赖 DeerMem 私有路径函数。

### 测试与文档

- `backend/tests/test_memory_storage_markdown.py`
  - 覆盖 agent 分桶、Markdown/manifest、thread 来源、物理删除、深拷贝、损坏拒绝、revision conflict、journal 恢复、repository、retrieval 委托/fallback、strict scope、自定义 manifest 和 v1→v2 迁移。
- `README.md`、`backend/AGENTS.md`
  - 同步 user/agent scope、Markdown canonical storage、journal/revision 和 retrieval adapter 行为。

## 4. 并发语义

临时文件 + replace 只保证单文件不会写一半，不等于并发安全。本实现组合使用：

1. 同进程 scope 锁；
2. 跨进程 scope 文件锁；
3. manifest `expected_revision` 比较；
4. 多文件 journal/recovery。

File backend 的承诺是单机本地文件系统上的“可检测、可恢复提交”，不是数据库事务。SQLite/Postgres 以后可以复用 repository 接口接入，不需要改写 updater 和 retrieval 契约。

## 5. Retrieval 边界

Storage 不实现 embedding、BM25、vector、MMR 或排序算法。检索同事提供的 adapter 工厂需满足 `RetrievalPort`；storage 只负责 canonical fact 生命周期通知、精确 user/agent scope 参数和 rebuild 数据源。未配置 adapter 时 capability/status 明确显示 `substring_fallback`。

## 6. 验证结果

- 记忆相关完整测试：`337 passed, 1 skipped`。
- Ruff lint：通过。
- Ruff format check：通过。
- `git diff --check`：通过。
- 当前实现代码中不存在 `project_id`、`projectId` 或 project 路径分桶；文档中的 project 仅用于说明本轮明确排除并留待独立 PR。
