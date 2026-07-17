# DeerMem Storage 改写计划：用户全局摘要与 Agent Facts 分离

> 状态：已按本计划实现；检索算法、主动召回和 project scope 不在本 PR。<br>
> 目标结构：`user_id → memory.json(user/history) + agent_name → fact*.md`。

## 1. 目标

1. 每个用户只有一个 `memory.json`。
2. `memory.json` 只保存项目无关、任何时候都适用的 `user/history` 摘要。
3. `memory.json` 不保存完整 fact，也不保存 fact ID、路径、hash 或 manifest。
4. 每条 fact 必须归属一个显式 `agent_name`，以单独 Markdown 保存。
5. global 读取不聚合所有 agent facts，避免项目记忆混用。
6. 显式 agent 读取返回“全局摘要 + 当前 agent facts”。
7. 前端现有 `/api/memory` schema 暂时保持 `facts: []`，避免缺字段报错。
8. 保留 revision、文件锁、journal/recovery、repository 和 retrieval adapter。

## 2. 非目标

- 不增加 `project_id`。
- 不实现 embedding、BM25、vector、MMR 或 rerank。
- 不决定用户主动提起时何时召回。
- 不改造 prompt injection 策略。
- 不引入 SQLite/Postgres。
- 不设计新的 agent fact 前端管理页面。

## 3. 数据模型

### 3.1 用户全局 JSON

```json
{
  "version": "2.0",
  "revision": 12,
  "lastUpdated": "2026-07-17T10:00:00Z",
  "user": {},
  "history": {}
}
```

允许字段仅为：

- `version`
- `revision`
- `lastUpdated`
- `user`
- `history`

禁止字段：

- `facts`
- `factManifest`
- `embeddings`
- 项目或 agent 专属摘要

### 3.2 Agent fact

```json
{
  "id": "fact_01...",
  "schemaVersion": 2,
  "content": "Project uses Python 3.12",
  "title": "Runtime constraint",
  "category": "constraint",
  "categoryExtension": null,
  "topics": ["python", "runtime"],
  "confidence": 0.95,
  "status": "active",
  "scope": {
    "userId": "alice",
    "agentName": "research-agent"
  },
  "source": {
    "type": "conversation",
    "threadId": "thread_123"
  },
  "createdAt": "2026-07-17T10:00:00Z",
  "updatedAt": "2026-07-17T10:00:00Z",
  "revision": 1,
  "consolidatedFrom": []
}
```

核心 category：

```text
preference / correction / context / goal / behavior /
identity / constraint / decision / other
```

未知类别迁移为：

```text
category=other
categoryExtension=<旧类别>
```

`thread_id` 只进入 `source.threadId`，不是存储 scope。

## 4. 目录结构

```text
{root}/users/{uid}/
├── memory.json
├── .memory.lock
├── .memory.journal.json
├── .recovery/
└── agents/{agent_name}/
    └── facts/{fact_id前两位}/{fact_id}.md
```

规则：

- `agent_name` 必须通过路径安全校验。
- fact ID 只允许 `[A-Za-z0-9_-]+`。
- `manifest_filename` 配置名暂时保留兼容，但语义变为用户全局摘要 JSON 文件名。
- `strict_user_scope=true` 时，无 user_id 直接报错。

## 5. 读写语义

### 5.1 Global 读取

```text
load(user_id, agent_name=None)
→ 读取 memory.json
→ 不扫描 agents/
→ 返回 user/history + facts=[]
```

### 5.2 Agent 读取

```text
load(user_id, agent_name=A)
→ 读取 memory.json 的 user/history
→ 扫描 agents/A/facts/**/*.md
→ 返回 user/history + A 的 facts
```

禁止跨 agent 自动聚合。

### 5.3 Global 写入

```text
save(document, agent_name=None)
```

- 只允许更新 `user/history`。
- facts 必须为空。
- 写入 JSON 时无论如何都不产生 `facts` key。

### 5.4 Agent 写入

```text
save(document, agent_name=A)
```

- 只修改 A 的 Markdown facts。
- 保留磁盘上现有 global `user/history`。
- 不创建 `agents/A/memory.json`。
- 其他 agent facts 不受影响。

### 5.5 Repository

```text
get_fact(user, agent, id)
list_facts(user, agent, filters, cursor, limit)
upsert_fact(user, agent, fact, expected_revision)
delete_fact(user, agent, id, expected_revision)
apply_changes(user, agent, change_set, expected_revision)
get_summaries(user)
update_summaries(user, summaries, expected_revision)
```

所有 fact 写操作必须提供 agent。

## 6. 前端兼容

前端 `UserMemory` 目前要求 `facts` 数组。暂不修改整个页面和 API contract：

- global storage load 在内存中补 `facts=[]`；
- Gateway 的 `MemoryResponse.facts` 保持 `default_factory=list`；
- export 返回空 facts；
- global import 接受旧 facts 字段但忽略它，只导入 user/history。

新的 agent fact 管理 UI 需在后续工作中明确 agent owner 后再设计。

## 7. 并发和恢复

### 7.1 Revision

一个用户共享一个 revision。任何 global summary 或任一 agent fact 提交都会加一。

优点：

- 语义简单；
- 能防止 global 与 agent、agent 与 agent 的静默覆盖。

代价：

- 不同 agent 并发写也可能冲突。

### 7.2 锁

- 同进程缓存/操作使用 Python `RLock`；
- 跨进程使用用户根目录 `.memory.lock`；
- Windows 使用 `msvcrt`，Unix 使用 `fcntl`；
- 超时由 `file_lock_timeout_seconds` 控制。

### 7.3 Journal

prepared journal 保存：

- agentName；
- expected/next revision；
- 本次 fact IDs；
- 旧 fact 路径；
- recovery 快照。

`prepared` 遗留时回滚；`committed` 遗留时保留新版本并清理。

## 8. 迁移

### 8.1 用户根目录旧 facts

若旧 `memory.json` 的 `facts` 是 list 或旧 manifest mapping，显式对某个 agent 执行首次 load/migrate 时：

1. 读取旧 facts；
2. 将其归到调用时给出的 agent；
3. 写成 Markdown；
4. 重写全局 JSON，删除 `facts` 字段。

因为旧 global facts 本身没有可靠 agent owner，迁移必须显式提供 agent_name。

### 8.2 旧 per-agent JSON

若存在：

```text
users/{uid}/agents/{agent}/memory.json
```

首次读取该 agent 时自动：

1. 提取旧 facts；
2. 写入同 agent Markdown 目录；
3. 以用户根目录全局摘要为准；
4. 删除旧 per-agent JSON。

## 9. Retrieval 边界

Storage 提供：

- fact upsert/remove 生命周期通知；
- 精确 `userId + agentName` scope；
- Markdown path；
- scoped search delegation；
- rebuild index 扫描源；
- capability/status。

Retrieval 负责：

- 索引格式；
- embedding/BM25/vector；
- 时间衰减和排序；
- 主动召回触发；
- 项目内召回与 prompt injection 协作。

没有 adapter 时只提供 substring fallback。

## 10. 验收条件

- [x] `memory.json` 不含 facts 或 fact manifest。
- [x] 一个 user 只有一个 global memory JSON。
- [x] fact 必须保存在显式 agent 目录。
- [x] global API 仍返回 `facts=[]`，前端读取不报错。
- [x] agent load 只返回当前 agent facts。
- [x] agent save 不覆盖 global summaries。
- [x] global updater 不保存无 owner facts。
- [x] revision conflict 被明确拒绝。
- [x] journal 可恢复 prepared 操作。
- [x] 旧 per-agent JSON 可迁移并删除。
- [x] retrieval 不依赖 JSON fact manifest。
- [x] project scope 未混入本 PR。

## 11. 已知限制

1. global summary 是否真正“项目无关”仍依赖提取 prompt/策略。
2. global fact CRUD 页面没有 agent owner，因此当前不适合管理 agent facts。
3. user 共享 revision 会让不同 agent 写入互相冲突。
4. File backend 列举 facts 需要扫描目录。
5. retrieval 通知是 storage commit 后的最终一致行为；失败时需 rebuild。
6. 本计划不提供跨机器分布式锁或数据库事务。
