# DeerMem Storage 改写实施说明

> 实施依据：[`STORAGE_REWRITE_PLAN.md`](STORAGE_REWRITE_PLAN.md)  
> 完成日期：2026-07-17  
> 实施范围：File/JSON + 单 fact Markdown；不引入 SQLite，不实现 retrieval 内部算法。

## 1. 最终存储结构

记忆使用精确作用域 `(user_id, agent_name, project_id)`。`thread_id` 只记录在 fact 的 `source.threadId` 中，不参与分桶。

```text
{storage_root}/
└── users/{user_id}/
    ├── memory.json
    ├── facts/{id前两位}/{fact_id}.md
    ├── agents/{agent_name}/...
    └── projects/{project_id}/...
```

当 agent 和 project 同时存在时，project 目录位于对应 agent 目录内。`memory.json` 只保存 user/history 摘要、manifest revision，以及 fact 的相对路径、revision、内容 hash；完整 fact 内容只以 Markdown 为准。

单 fact Markdown 的 YAML front matter 保存结构字段，例如 ID、schemaVersion、核心类别、扩展类别、topics、confidence、status、scope、source、时间、revision 和 consolidation 来源；正文使用一级标题和自然语言内容，便于人工阅读与外部检索模块解析。

## 2. 修改位置与功能

### 2.1 Storage 核心

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/storage.py`
  - 将文档版本提升到 `2.0`，保留旧 `load/save` 文档形状作为兼容层。
  - 实现单 fact Markdown 渲染、解析、YAML front matter、SHA-256 校验和 JSON manifest。
  - 实现单 fact repository：`get_fact`、`list_facts`、`upsert_fact`、`delete_fact`、`apply_changes`、`get_summaries`、`update_summaries`。
  - 删除为物理删除；批量新增、更新、删除和摘要变更共用 `apply_changes`。
  - 增加 manifest/fact revision、`expected_revision` 乐观并发检查、同 scope 线程锁和跨进程文件锁。
  - 多文件提交增加 prepared/committed journal 和 `.recovery` 快照；中断后首次访问自动回滚未完成提交。
  - 区分数据不存在与数据损坏；损坏的 JSON、Markdown、ID 或 hash 会抛出明确异常，不再当作空记忆覆盖。
  - 缓存键升级为 `(user_id, agent_name, project_id)`；返回深拷贝；校验使用纳秒 mtime + 文件大小。
  - 增加显式 `migrate()`，旧 JSON fact 数组也会在首次访问时幂等迁移。
  - 增加 retrieval port、upsert/remove 通知、`search_facts`、`rebuild_index`、`retrieval_status` 和 `capabilities`。
  - 未配置 retrieval 时明确报告并使用 `substring_fallback`，不再静默返回空结果。
  - `storage_class` 和 `retrieval_adapter` dotted path 加载失败时 fail-fast，避免持久化后端静默切换。

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/paths.py`
  - 新增安全的内部 `project_id` 校验和 project 分桶。
  - 新增 fact ID 校验及 `facts/{prefix}/{id}.md` 分片路径。
  - 支持可配置的纯文件名 `manifest_filename`，拒绝路径穿越形式。
  - 支持 `strict_user_scope`；启用后缺少 `user_id` 会直接失败。

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/config.py`
  - 新增 `storage_class`、`strict_user_scope`、`fact_format`、`manifest_filename`、`file_lock_timeout_seconds`、固定启用的 `journal_enabled`、`retrieval_adapter`。
  - 所有字段均有兼容默认值；本轮 `fact_format` 固定为 Markdown。

- `config.example.yaml`
  - 在 `memory.backend_config` 中补齐上述 storage/retrieval 配置及用途注释。

### 2.2 Updater、队列和 Manager 契约

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/updater.py`
  - 所有读写增加 `project_id`。
  - updater 将前后文档计算为 upsert/delete/summaries change set，通过 repository 的 `apply_changes` 提交。
  - 旧 storage provider 仍可走兼容 `save`；File provider 使用 revision 拒绝陈旧写入。

- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/queue.py`
  - 队列身份升级为 `(thread_id, user_id, agent_name, project_id)`，防止相同 thread 字符串在不同 scope 合并。
  - project 在计时器/后台线程边界前捕获并传给 updater。

- `backend/packages/harness/deerflow/agents/memory/manager.py`
- `backend/packages/harness/deerflow/agents/memory/backends/noop/noop_manager.py`
- `backend/packages/harness/deerflow/agents/memory/backends/deermem/deer_mem.py`
  - Manager ABC、noop 和 DeerMem 的公开方法统一增加可选 `project_id`；project ID 校验也位于 Manager 契约，Gateway 不依赖 DeerMem 私有路径函数。
  - DeerMem search 优先调用 storage/retrieval 接口，并兼容外部 `SearchResult.fact` 和旧 fact 直返格式。

### 2.3 Runtime scope 贯通

- `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py`
- `backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py`
- `backend/packages/harness/deerflow/agents/memory/summarization_hook.py`
- `backend/packages/harness/deerflow/agents/memory/tools.py`
- `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
  - middleware 更新、紧急 flush、动态注入、lead prompt 和 tool CRUD/search 都从 runtime context 读取同一个内部 `project_id`。

- `backend/packages/harness/deerflow/runtime/context_compaction.py`
- `backend/app/gateway/routers/threads.py`
  - 手动 compact 请求可携带内部 project ID，并在压缩摘要写入记忆时保留 scope。

- `backend/packages/harness/deerflow/runtime/runs/worker.py`
  - runtime 合并 caller context 时校验 `project_id` 只包含内部 ID 允许的安全字符。

- `backend/packages/harness/deerflow/subagents/executor.py`
- `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
  - task 创建 subagent 时继承父运行的 project ID，避免子 agent 写入无 project 或错误 project 的记忆桶。

### 2.4 Gateway API 与 schema

- `backend/app/gateway/routers/memory.py`
  - `/api/memory*` 支持 `?project_id=<内部ID>`，通过当前 `MemoryManager.validate_project_id()` 校验，插件拒绝时返回 HTTP 400。
  - 未传 project 时不向旧 provider 强行传 `project_id=None`，保持插件兼容。
  - Fact API schema 可表达 schema v2 的 scope、structured source、topics、status、revision、扩展类别与 consolidation 字段，同时继续读取/返回旧 fact。

### 2.5 文档与测试

- `README.md`：更新用户可见的 scope、Markdown、journal/revision 和 retrieval fallback 行为。
- `backend/AGENTS.md`：更新 storage 架构、目录、配置和开发约束。
- `backend/tests/test_memory_storage_markdown.py`：覆盖 project 分桶、Markdown/manifest、深拷贝、损坏拒绝、revision conflict、物理删除、journal 恢复、repository、fallback、严格 scope、自定义 manifest 和迁移。
- `backend/tests/test_memory_storage_user_isolation.py`：缓存键断言升级为三元 scope。
- `backend/tests/test_memory_tools.py`、`backend/tests/test_memory_queue.py`：覆盖动态 project 的工具和队列传播。

## 3. 并发与故障语义

单文件的临时文件 + replace 只能保证“该文件不会写一半”，不能防止两个 writer 基于同一旧版本相互覆盖。本实现增加三层保护：

1. 同进程同 scope 的 `RLock`；
2. 多进程同 scope 的 advisory file lock；
3. manifest `expected_revision` 比较，陈旧 writer 抛出 `MemoryRevisionConflict`。

Markdown 和 manifest 是多文件提交，仍不具备数据库事务。journal 的承诺是“可恢复”：prepared 阶段中断后恢复旧 manifest/facts，committed 阶段只做清理；不会把未知的半提交状态当作成功。

## 4. 当前明确边界

- 未使用 SQLite，也未新增数据库依赖。当前规模下 File backend 足够；当多机共享存储、高写并发或复杂查询成为硬需求时，可通过稳定 repository 接口替换为 SQLite/Postgres。
- retrieval 的 embedding、BM25、vector、MMR 和排序不属于本改动。`retrieval_adapter` 工厂需由检索同事实现，并满足 storage 中的 `RetrievalPort`；未配置时使用明确的 substring fallback。
- 当前 Gateway 只验证 project ID 的格式。project 是否存在、当前用户是否是成员，仍应由未来的 project service 在把内部 ID 放入 runtime context 前鉴权。
- Markdown 标题是人类可读的 fact 标题；时间、topics、scope 等机器过滤字段放在 YAML front matter，不把多个字段拼进标题，避免标题不稳定和解析歧义。

## 5. 验证结果

- `ruff format`：通过。
- `ruff check`（24 个涉及的 Python 文件）：通过。
- 全部 memory 定向回归：`262 passed, 2 skipped`。
- runtime/compact/subagent/task/threads/summarization 定向回归：`220 passed`。

测试仅出现环境相关警告：当前工作区禁止 pytest 写 `.pytest_cache`，以及 Starlette 关于未来 `httpx2` 的弃用提示；两者均不影响测试结果。
