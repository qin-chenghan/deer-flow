# DeerMem Storage 改写计划书

> 状态：File/Markdown storage 按本方案实施；project scope 已从本轮撤回，留待独立 PR。检索算法及最终联调签名仍待 retrieval 负责人锁定。实施位置见 [`STORAGE_REWRITE_CHANGES.md`](STORAGE_REWRITE_CHANGES.md)。<br>
> 范围：`backends/deermem/deermem/core/` 内的 storage、paths、schema、索引生命周期和后端适配；同时列出 updater、retrieval、injection、queue、Gateway 必须配合的契约。<br>
> 基线：2026-07-16 当前代码。本文为描述式规格，不包含实现代码。

## 1. 背景与目标

当前 DeerMem 以 `memory.json` 为主要持久化载体。现有实现已经具备：

- 按 `user_id` 分桶；
- custom agent 场景下按 `agent_name` 分桶；
- 临时文件写入后 `replace()` 的单文件原子替换；
- 进程内、按 `(user_id, agent_name)` 分隔的 mtime 缓存；
- 可配置 `storage_class`，允许接入其他 storage 实现；
- fact 的自动抽取、人工 CRUD、staleness、contradiction 和 consolidation。

但它还不能满足即将接入的检索模块和多 worker 场景。此次改写目标是：

1. 固化现有 user/agent scope，并确保 thread 只作为来源，阻止现有隔离维度间的记忆混用；
2. 将 storage 从“整份文档读写器”升级为支持单 fact CRUD、事务和查询的 repository；
3. 采用 File/JSON + 单 fact Markdown：Markdown 是 fact 的 canonical storage，主 JSON 保存摘要、manifest 和文档级状态；
4. 暂不引入 SQLite，继续保留旧 `memory.json` 的兼容、迁移和回退能力；
5. 明确 fact schema、版本迁移和索引生命周期；
6. 检索由独立 retrieval 模块负责，storage 通过注入的检索端口通知建索引，并由 `search` 委托其查询 API；
7. 为以后接入 SQLite/Postgres 留出稳定接口，但不作为本轮实现内容；
8. 保持 DeerMem 可移植：core 内零 `deerflow.*` import，依赖通过配置和构造函数注入。

## 2. 非目标

本轮不负责完整实现以下功能，但必须预留契约：

- 检索引擎、向量数据库、BM25、vector 和 MMR 的具体实现；
- SQLite/Postgres 后端；
- 前端 memory 管理页面重做；
- message importance scorer；
- injection token 策略重做；
- 在 storage 内实现检索算法。

## 3. 当前问题清单

### 3.1 project 没有进入持久化作用域

当前主要作用域为 `(user_id, agent_name)`。`thread_id` 只作为 fact 来源信息使用，不参与文件分桶；`project` 不存在。同一用户使用默认 lead agent 处理多个项目时，项目技术栈、限制和决策会进入同一份记忆，检索和 contradiction/consolidation 也会跨项目发生。

### 3.2 隔离依赖可选参数，缺参会退回共享库

`user_id=None` 会进入 legacy 全局路径；`agent_name=None` 会进入用户全局库。调用链漏传身份时不会必然报错，可能静默写错 scope。生产环境应对缺少用户身份采取 fail-closed，而不是共享回退。

### 3.3 原子替换不等于并发安全

临时文件加 `replace()` 只能避免半截 JSON，不能保护完整的 read-modify-write：

```text
Worker A load v10 ──新增 A──▶ save v11
Worker B load v10 ──新增 B──▶ save v11（覆盖 A）
```

当前锁仅保护单个 Python 实例中的缓存和队列，不能覆盖多个 manager 实例、多个进程、Gateway 多 worker、人工 CRUD 与后台 updater 的竞争。因此存在 lost update。

### 3.4 storage 接口粒度过粗

现有核心接口只有 `load/reload/save`，每次处理整份 memory dict。它无法自然表达：

- 单 fact upsert/delete；
- 条件查询和分页；
- revision/乐观锁；
- 数据库事务；
- 唯一约束和幂等；
- scope 过滤；
- FTS/向量索引；
- audit log 与 tombstone；
- 多 fact consolidation 的原子提交。

若不先改接口，SQLite/Postgres 最终也只能保存一个大 JSON blob，无法发挥数据库能力。

### 3.5 缓存暴露可变对象

File storage 命中缓存后返回原 dict 引用。调用者若原地修改，可能在持久化成功前污染缓存。storage 应返回副本或不可变模型，并明确所有写操作必须走 repository 方法。

### 3.6 文件损坏被当作空记忆

JSON 解析或 IO 失败时当前实现返回空 memory。后续一次成功保存可能覆盖原有损坏文件，造成不可恢复的数据丢失。损坏、暂时不可读和“文件不存在”必须是三种不同状态。

### 3.7 队列和写入是进程内 best-effort

`threading.Timer` 队列在进程退出时可能丢任务；多 worker 各自持有队列和缓存；失败没有可靠重试/死信。它不属于 storage 单插件的全部责任，但 storage 必须提供队列持久化可复用的事务和连接边界。

### 3.8 custom agent memory 的管理入口不完整

运行链路支持 agent scope，但 Gateway memory CRUD 主要操作用户全局 memory。管理端、检索端和运行端可能看到不同库，后续 API 必须明确 scope，而不能默认只管理全局库。

### 3.9 当前检索只是全量 substring

现有 search 会加载全部 facts，做字符串包含匹配再按 confidence 排序。它不能提供可靠的 BM25、向量、时间衰减和 MMR，也不能承担大规模数据。本轮不在 storage 内实现这些算法，而是定义稳定的委托接口，调用 retrieval 模块负责人提供的 API。

## 4. 总体设计原则

1. **先隔离，后排序**：检索必须先按 scope 做硬过滤，再计算 BM25/向量相似度。
2. **单一事实源**：单 fact Markdown 是 fact 正文与 fact metadata 的唯一事实源；主 JSON 不重复保存完整 fact。
3. **变更集一致性**：File backend 没有数据库事务，因此用 manifest revision、操作日志、临时文件和恢复流程保证多文件更新可检测、可恢复。
4. **职责分离**：storage 负责 fact 文件生命周期；retrieval 模块负责索引和搜索算法。updater 只调用 storage，storage 通过注入的 retrieval port 发送 upsert/remove/rebuild 请求。
5. **reader 向后兼容，writer 写最新版本**：旧字段缺失可读；迁移后统一写最新 schema。
6. **持久化配置错误 fail-fast**：错误 backend、路径或 schema 不得静默回退到另一存储。
7. **可选运行时能力优雅降级**：embedding 客户端不可用时关闭 vector，不影响结构化存储和关键词检索。
8. **兼容迁移而非双写**：旧 File/JSON 数据可读取和迁移；切到 v2 后以 Markdown facts + JSON manifest 运行，不长期维护 v1/v2 双写。
9. **可移植 core**：路径、schema、storage、索引代码只使用相对 import，不读取 DeerFlow host 单例。

## 5. Scope 设计

### 5.1 统一模型

所有调用链统一携带下面的上下文：

```text
MemoryContext = (user_id, agent_name, thread_id)
```

建议语义如下：

| 字段 | 是否允许为空 | 含义 |
|---|---:|---|
| `user_id` | 仅 legacy/显式匿名模式允许 | 数据所有者和安全隔离边界 |
| `agent_name` | 是 | `None` 表示用户公共层；非空表示 agent 专属层 |
| `thread_id` | 是 | 仅表示事实来源，不作为长期记忆分桶或检索隔离键 |

其中真正的长期记忆隔离键为：

```text
MemoryScope = (user_id, agent_name)
```

`thread_id` 写入 `source.threadId`，用于审计、溯源和删除来源关联，不参与目录分桶。上下文/scope 必须是结构化值对象，而不是各模块分别传散乱参数。它至少需要：

- 规范化与校验；
- 稳定的 equality/hash；
- 序列化为 DB 列；
- 生成日志安全的 scope key；
- 区分 `None` 和空字符串；
- 禁止路径穿越；
- 不把目录名当作授权依据。

### 5.2 分层读取规则

一次查询按现有两层 scope 读取：

1. 用户公共：`(user, None)`；
2. 当前 agent：`(user, agent)`；
3. `thread_id` 不形成单独长期记忆层，只作为候选的来源过滤条件（如确有业务需要）。

优先级建议为“精确 scope 高于公共 scope”，但 correction 等 guaranteed category 的最终策略归 injection 所有。Storage 只负责精确过滤和返回候选，不负责 token 截断。

### 5.3 `None` 的风险控制

- 生产模式默认禁止 `user_id=None` 写入；legacy 模式需显式配置开启。
- scope 必须写入 fact/DB 记录，即使目录已经隐含 scope，防止导入、移动和重建索引时丢失上下文。

### 5.4 Project scope 延后

Project scope 会修改 `MemoryManager` ABC、Gateway、runtime、tool、middleware、subagent、retrieval 和 injection，变更面明显大于 storage。本轮不增加 `project_id` 参数、不建立 project 目录，也不修改插件契约；后续以独立设计和 PR 实施。

## 6. Fact Schema v2（方向已接受，字段名在实现前冻结）

已接受以下整体结构。进入编码前需将字段名、类型、默认值写成 JSON Schema/Pydantic 等价定义并冻结。

### 6.1 建议的逻辑结构

```json
{
  "id": "fact_01...",
  "schemaVersion": 2,
  "content": "DeerFlow memory facts use Markdown as canonical storage",
  "title": "Markdown canonical memory facts",
  "category": "context",
  "categoryExtension": "architecture",
  "topics": ["memory", "storage", "isolation"],
  "confidence": 0.92,
  "status": "active",
  "scope": {
    "userId": "user_123",
    "agentName": "lead-agent"
  },
  "source": {
    "type": "llm",
    "threadId": "thread_456",
    "messageIds": ["msg_789"],
    "runId": "run_..."
  },
  "createdAt": "2026-07-16T10:00:00Z",
  "updatedAt": "2026-07-16T10:00:00Z",
  "expiresAt": null,
  "lastAccessedAt": null,
  "revision": 1,
  "consolidatedAt": null,
  "consolidatedFrom": [],
  "metadata": {}
}
```

### 6.2 字段分级

**必填：**

- `id`：稳定、全局唯一，不由正文生成；
- `schemaVersion`：fact 自身版本；
- `content`：供模型和检索使用的原子事实；
- `category`：核心枚举类别；
- `confidence`：0~1；
- `status`：至少 `active/deleted/superseded`；
- `scope`：`userId/agentName` 结构，字段可空但对象必须存在；
- `source.type`：`llm/rule/manual/consolidation/import`；
- `createdAt/updatedAt`；
- `revision`：乐观锁。

**可选：**

- `title`：短语义标题，可用于 Markdown 和 BM25；
- `categoryExtension`：扩展类别；当核心枚举无法准确表达时使用，但不能替代核心类别；
- `topics`：多值主题标签；
- `expiresAt`：明确过期时间，优于单独 ttl；
- `lastAccessedAt`：若启用读取热度策略；
- `consolidatedAt/consolidatedFrom`：兼容现有 consolidation；
- `metadata`：受大小限制的扩展字段。

**明确不进入 canonical fact：**

- `embedding` 数组：存 sidecar 索引表；
- message `importance`：属于 ConversationContext；
- 最终 retrieval score：每次查询动态产生；
- injection token 数：属于本次注入过程。

### 6.3 已定字段策略

1. `thread_id` 只作为 `source.threadId`，不属于长期 scope。
2. category 采用“核心枚举 + 扩展类别”：
   - 核心枚举建议第一版为 `preference/correction/context/goal/behavior/identity/constraint/decision/other`；
   - `categoryExtension` 是可选短字符串，例如 `architecture`、`deployment`；
   - 检索过滤优先使用核心枚举，扩展类别用于细分；
   - 未识别旧类别迁移为 `category=other, categoryExtension=<旧值>`，不丢信息。
3. 第一版不在每次读取时同步写 `lastAccessedAt`，避免检索产生写放大；字段可保留为空，后续由 retrieval 模块决定是否异步聚合。
4. 删除采用彻底物理删除：canonical Markdown、manifest 条目及检索索引均删除。为了可恢复和审计，删除前写 operation journal/audit，但不保留可被正常读取的 tombstone fact。
5. `source` 迁移为对象，同时 reader 接受旧字符串。
6. `metadata` 必须设置允许键策略和大小上限，不能替代正式 schema 字段。

## 7. 记忆文档 Schema 与迁移

### 7.0 主 JSON 与 fact Markdown 的职责

新布局中，主 JSON 不再保存完整 `facts: [...]` 正文数组。职责划分如下：

- **单 fact Markdown（canonical）**：保存 fact 全部结构字段、正文和 revision；
- **主 JSON（manifest/document state）**：保存 `version`、`lastUpdated`、user/history summaries、fact ID 清单、相对路径、revision、内容 hash 和恢复状态；
- **operation journal**：记录尚未完成的多文件变更集，用于崩溃恢复；
- **retrieval index**：由 retrieval 模块拥有，可从 fact Markdown 重建。

建议主 JSON：

```json
{
  "version": "2.0",
  "revision": 12,
  "lastUpdated": "2026-07-16T10:00:00Z",
  "user": {},
  "history": {},
  "facts": {
    "fact_01...": {
      "path": "facts/fa/fact_01....md",
      "revision": 1,
      "contentHash": "sha256:..."
    }
  }
}
```

这里的 `facts` 是 manifest，不重复保存 `content/topics/source` 等完整 fact 字段。读取事实时以 Markdown 为准；manifest 用于快速枚举、校验和恢复。

### 7.1 演进规则

- schema 只加不减；需要移除的字段先 deprecated；
- 顶层 `version` 每次不兼容语义变化必须 bump；
- reader 对缺字段使用明确默认值；
- writer 只写最新版本；
- `migrate()` 必须幂等，可重复执行；
- 每个迁移步骤单独测试：`v1 -> v2 -> current`；
- 导入前校验，迁移失败不得覆盖原文件。

### 7.2 损坏处理

必须区分：

- 文件不存在：返回空 memory；
- 文件格式旧：执行迁移；
- JSON/Markdown 损坏：进入 degraded/error，保留原文件，不允许自动覆盖；
- IO 暂时失败：向调用方返回可重试错误。

损坏文件建议复制或改名为只读备份，并在状态 API 暴露错误。不得把解析失败当作空库。

### 7.3 legacy `storage_path`

- 空值：factory 注入绝对 `runtime_home`；
- 非空：始终表示根目录；
- 若路径指向已存在文件：启动 fail-fast；
- 旧 `.json` 文件式配置：识别、告警并通过显式迁移处理；
- 不允许把 `memory.json` 当目录继续拼 `users/...`；
- 所有路径解析只在 core `paths.py` 中实现；
- v1 `memory.json` 中的 `facts` 数组迁移为逐条 Markdown，验证所有 hash 和计数成功后，再写 v2 manifest；
- 原 v1 JSON 保留备份，迁移过程不得边读边破坏源文件。

## 8. Storage 接口重构

### 8.1 分层接口

为兼容旧模块，建议分为两层：

**兼容文档层：**

```text
load(scope) -> MemoryDocument
reload(scope) -> MemoryDocument
save(document, scope, expected_revision?) -> SaveResult
```

**新 repository 层：**

```text
get_fact(scope, fact_id)
list_facts(scope, filters, cursor, limit)
upsert_fact(scope, fact, expected_revision)
delete_fact(scope, fact_id, expected_revision)
apply_changes(scope, change_set, expected_manifest_revision)
get_summaries(scope)
update_summaries(scope, summaries, expected_revision)
```

其中 `apply_changes` 用于一次 updater 产生多个新增、更新、物理删除和 consolidation。File backend 无法提供真正的多文件原子事务，所以其语义是“带 journal 的可恢复提交”：要么完成全部步骤，要么下次启动/访问时根据 journal 前滚或回滚，绝不能静默停在未知状态。

### 8.2 索引接口

S6 建议锁定为：

```text
notify_fact_upsert(scope, fact_or_path)
notify_fact_remove(scope, fact_id)
search_facts(query, *, scopes, top_k, mode, filters) -> list[SearchResult]
rebuild_index(scopes=None) -> RebuildReport
retrieval_status() -> RetrievalStatus
capabilities() -> set[str]
```

说明：

- `notify_fact_upsert` 委托注入的 retrieval port，必须是幂等 upsert；
- `notify_fact_remove` 对不存在的索引记录应幂等成功；
- `search_facts` 接受一个或多个精确 scope，由 retrieval 决定分层查询；
- `mode` 至少预留 `keyword/vector/hybrid`；
- `SearchResult` 包含 fact、score、match type，不包含 injection 决策；
- `rebuild_index` 遍历 canonical Markdown 并委托 retrieval，用于存量回填、模型更换和灾难恢复；
- retrieval port 未配置时，`search_facts` 可临时回退当前 substring，且 capability/status 必须明确显示 fallback；
- storage 不实现 embedding、BM25、MMR 或向量表。

### 8.3 FileMemoryStorage 行为

File backend 继续可运行，但能力必须诚实声明：

- 保留 JSON 文档读写；
- 索引写操作可 no-op；
- `search_facts` 回退为内存 substring/BM25，或明确声明不支持；
- 不允许“返回空列表且无能力标记”的静默失败；
- 返回数据使用 deep copy；
- 同进程对同 scope 的完整更新使用锁；
- 可选跨进程文件锁只作为过渡，不把它宣传为数据库级事务；
- revision/hash 不匹配时拒绝覆盖并要求重试。

### 8.4 后端选择与错误策略

- `storage_class: file`：官方短名；
- dotted path：第三方实现；
- 空值：使用 File/JSON + fact Markdown 默认后端；
- 短名/dotted path 解析失败必须 fail-fast，不能回退 File；
- `noop` 后端同步全部 ABC/能力契约。

## 9. File/JSON + fact Markdown 后端方案

### 9.1 目录布局

```text
{storage_path}/users/{uid}/
├── memory.json                         # 用户公共 manifest + summaries
├── facts/{prefix}/{fact_id}.md         # 用户公共 facts
└── agents/{agent_name}/
    ├── memory.json                     # agent 公共层
    └── facts/{prefix}/{fact_id}.md
```

`thread_id` 不出现在路径中。`prefix` 使用 fact ID 的稳定前缀，避免单目录文件数无限增长。

### 9.2 单 fact 操作

新增/更新一条 fact：

1. 读取 manifest revision；
2. 校验目标 fact revision；
3. 写 operation journal；
4. 将完整 Markdown 写入唯一临时文件并 fsync（平台支持时）；
5. 原子 replace 目标 fact 文件；
6. 写入新版 manifest 临时文件并 replace；
7. 标记 journal committed；
8. 调用 retrieval port 的幂等 upsert；
9. 清理 journal。

物理删除一条 fact：

1. 写 journal，并记录目标路径、旧 hash/revision；
2. 将文件移动到本次操作的临时 recovery 区，而不是第一步直接永久删除；
3. 更新 manifest 并原子 replace；
4. 调 retrieval port remove；
5. commit 后永久清理 recovery 文件。

对正常读取而言这是彻底删除；短暂 recovery 文件只用于崩溃恢复，不参与检索和注入。

### 9.3 多 fact 变更集

consolidation、staleness、clear 等可能同时改多个文件。提交前必须在 journal 记录完整 change set、旧 manifest revision、各文件旧/新 hash。恢复器根据阶段执行前滚或恢复旧文件。主 JSON 的 revision 是整个 scope 的 compare-and-swap 条件。

这仍不等于数据库事务，但比单纯 `replace(memory.json)` 更可检测、可恢复。所有调用方遇到 revision conflict 必须重新读取后重试，不能 last-write-wins。

### 9.4 多进程锁

- 进程内按 scope 使用互斥锁；
- 多 worker 使用跨进程、按 scope 的 advisory file lock；
- lock 文件路径由 `paths.py` 统一生成；
- 锁必须有超时和 owner 信息；
- 网络文件系统上的 file lock 语义不可靠，本方案只承诺单机本地磁盘；
- LLM 和 retrieval 网络调用必须发生在锁外；
- 写锁内只做校验、文件替换、manifest 更新和 journal 状态推进。

## 10. Markdown 单 fact 主存储

### 10.1 定位

单 fact 单 Markdown 是 fact 的 canonical storage。主 JSON 只保存摘要和 manifest，不保存完整 fact 正文。因此 Markdown 丢失不能仅靠主 JSON 恢复，必须依赖备份/journal；检索索引则必须能从 Markdown 重建。

### 10.2 格式

时间、scope、topic 等机器字段使用 YAML front matter，标题只表达语义：

```markdown
---
id: fact_01...
schema_version: 2
user_id: user_123
agent_name: lead-agent
thread_id: null
category: architecture
topics: [memory, storage]
created_at: 2026-07-16T10:00:00Z
updated_at: 2026-07-16T10:00:00Z
revision: 1
---

# Markdown canonical memory facts

DeerFlow memory facts use Markdown as canonical storage.
```

不使用多层标题代替 metadata，也不把 embedding 写进 front matter。

### 10.3 一致性规则

- 文件名使用稳定 fact ID，不使用可变标题；
- Markdown 是 fact 事实源，主 JSON 是 manifest/summaries；二者不重复保存完整 fact；
- 第一版不允许绕过 API 直接编辑；人工编辑通过显式 import，完成 schema、scope、revision 和 hash 校验；
- 文件带 revision/content hash；
- 写临时文件后原子 replace；
- fact 文件与 manifest 更新失败时按 journal 恢复，不允许只成功一边；
- 删除 fact 时物理删除 canonical 文件、移除 manifest 条目并通知 retrieval 清理索引；
- 大量文件按 scope 和 ID 前缀分片；
- Markdown loader 必须校验 front matter scope 与目录/manifest 一致，任何不一致进入 degraded 状态，不能任意选择一个值。

## 11. 写入、读取与紧急路径

### 11.1 WRITE

```text
middleware
→ message_processing
→ queue
→ updater（LLM 在事务外）
→ write gate
→ storage.apply_changes（scope lock + journal + 原子文件替换）
→ retrieval port 通知
```

updater 只提交业务 change set，不直接写 FTS、embedding 或 Markdown。

### 11.2 READ

```text
lead_agent
→ injection（计算要查的 scope 层）
→ retrieval
→ storage.search_facts（委托 retrieval API；scope 硬过滤）
→ ranked candidates
→ injection（唯一 token 预算点）
```

retrieval 未启用或 backend 不支持时，injection 回退 `storage.load`，保持现状可运行。

### 11.3 EMERGENCY

```text
summarization_hook
→ message_processing
→ queue.add_nowait
→ updater
→ storage.apply_changes
```

紧急路径与普通写路径必须共用同一 scope、schema、write gate 和事务提交逻辑，不另造一套保存方式。

## 12. 并发、一致性与恢复

### 12.1 乐观并发

- 文档和 fact 都有 revision；
- updater 基于 revision N 生成 change set；
- 提交时若当前已不是 N，返回 conflict；
- 调用方重新 load，并重新应用确定性变更或重新运行 merge；
- 不允许 last-write-wins 静默覆盖。

### 12.2 幂等

- fact ID 在生成后稳定；
- index/upsert/delete 可重复执行；
- queue job 有 operation ID；
- consolidation change set 有唯一 operation ID；
- retry 不产生重复 fact。

### 12.3 Audit 与备份

- File backend 将 change set 写入按 scope 管理的 operation/audit journal；
- consolidation 保留 `consolidatedFrom`；
- import、clear、批量 delete 必须有 audit；
- File backend 在迁移/批量操作前生成备份；
- 明确 retention 与清理策略，不能无限增长。

## 13. 配置草案

全部新字段进入 `DeerMemConfig`，必须有默认值，并在 `config.example.yaml` 的 `memory.backend_config` 下记录。示意：

```yaml
memory:
  backend_config:
    storage_class: file          # file | dotted.path.Class
    storage_path: ""             # root directory
    strict_user_scope: false     # 兼容默认；生产建议 true
    fact_format: markdown        # 本轮固定 markdown，预留兼容字段
    manifest_filename: memory.json
    file_lock_timeout_seconds: 10
    journal_enabled: true
    retrieval_adapter: ""        # 由 host/DI 注入或 dotted path
```

最终字段名须在 Wave 1 评审时锁定。新增配置同步三处：`DeerMemConfig`、legacy 迁移映射（若有）、`config.example.yaml`。

## 14. 实施阶段

### Phase 0：契约锁定与基线测试

1. 锁定现有 `MemoryScope=(user_id, agent_name)` 和 source `thread_id` 语义；
2. 将已接受的 fact schema v2 固化为机器可校验 schema；
3. 与 retrieval 负责人锁定委托 API 签名、错误语义和返回类型；
4. 建立当前 File 行为的回归测试；
5. 建立并发 lost-update 复现测试；
6. 记录现有 JSON fixture 和迁移样本。

**退出条件**：S1/S2/S3/S6 文档签字确认，后续模块不得自行新增字段或改签名。

### Phase 1：Schema、Scope 与 File 后端加固

1. 新增 core `MemoryScope` 和校验；
2. 更新 paths 分桶，但保留 legacy 路径读取/迁移；
3. 实现 version-driven `migrate()`；
4. File load 返回副本；
5. 区分 missing/corrupt/IO error；
6. 增加 manifest/fact revision、hash 和 scope file lock；
7. 实现 operation journal 与崩溃恢复；
8. 暴露 repository 和 retrieval 委托接口；
9. File 无 retrieval 时的 fallback 行为显式 capability 化；
10. 同步 noop backend。

**退出条件**：零配置行为兼容；跨 user/agent/thread 测试通过；损坏数据不会被覆盖。

### Phase 2：单 fact Markdown canonical storage

1. 实现 YAML front matter + Markdown renderer/parser；
2. 实现主 JSON manifest 和 summaries；
3. 实现 `apply_changes` 的 journal/recovery 提交协议；
4. 实现单 fact 新增、更新、物理删除和批量 consolidation；
5. 实现旧 JSON facts 数组 → Markdown + manifest 的 dry-run/import/verify/rollback 工具；
6. 实现目录/manifest/front matter/hash 一致性扫描和修复报告；
7. storage_class 解析错误继续 fail-fast。

**退出条件**：单机多进程并发测试无静默 lost update；崩溃可恢复；迁移前后数据、scope、fact 数和 hash 一致。

### Phase 3：Updater 与索引生命周期接线

1. updater 输出 change set；
2. persist 后由 storage 通过 retrieval port 通知事实 upsert/remove；
3. consolidation、staleness、contradiction、manual CRUD 全部走同一事务；
4. 实现 retrieval 通知重试/status/rebuild；
5. `DeerMem.search` 经 storage adapter 调 retrieval 负责人暴露的 API；
6. embedding 不进入 canonical fact。

**退出条件**：新增、更新、删除、合并、清理后，canonical facts 与 retrieval 可见事实集合一致；重建结果一致。具体采用 FTS、向量还是混合索引不由 storage 规定。

### Phase 4：Retrieval 集成联调

1. 与 retrieval 负责人实现 injected adapter；
2. 定义 upsert/remove/search/rebuild 的超时、幂等和错误语义；
3. 传递精确的 user/agent scope 过滤条件；
4. retrieval 不可用时按配置 fallback 或明确报 degraded；
5. 全量扫描 Markdown 触发 rebuild；
6. embedding、vector、BM25、MMR 细节全部留在 retrieval 模块。

**退出条件**：删除全部检索索引后，可从 canonical Markdown 完整重建；storage 不含检索算法实现。

### Phase 5：动态 project 管理 API 与前端配合（独立 PR，不属于本轮）

1. thread/project 建立稳定归属；
2. Gateway/runtime/tool/middleware/subagent 传播 project；
3. memory CRUD API 显式接受和校验 scope；
4. 前端可以选择用户公共、project 和 agent memory；
5. 授权检查基于服务端 project membership，而不是路径字符串。

**退出条件**：同一用户两个 project 的写入、检索、注入、管理完全隔离；公共偏好仍可按设计共享。

## 15. 测试与验收

### 15.1 Schema/迁移

- v1 缺 id、缺 consolidation 字段仍可读；
- v1→v2 幂等；
- 未知未来字段不丢失或按明确策略保留；
- corrupt 文件不被空数据覆盖；
- import 校验失败无部分写入。

### 15.2 Scope 安全

- user A 永远检索不到 user B；
- agent A/B 按分层规则隔离；
- thread scope 不误入长期公共层；
- 缺失 user 在 strict 模式拒绝写；
- 路径穿越和特殊字符归一化安全；
- 检索先过滤 scope，不能先全库 top-k 后过滤。

### 15.3 并发

- 两 worker 同时新增不同 fact，二者均保留；
- 同 fact 并发修改触发 revision conflict；
- updater 与 manual CRUD 并发不丢数据；
- consolidation 多 fact 更新全成或全败；
- scope file lock 有超时，不无限阻塞；
- revision conflict 不静默覆盖；
- 在每个 journal 阶段模拟进程崩溃后，均可前滚或恢复到一致状态。

### 15.4 索引

- index/upsert/delete 幂等；
- 删除后不再可搜；
- consolidation 后旧 fact 不再可搜，新 fact 可搜；
- retrieval 索引重建前后结果集合一致（具体排名验收由 retrieval 模块负责）；
- retrieval API 不可用时 fallback/degraded 行为符合配置；
- File backend 的 capability 与实际行为一致。

### 15.5 兼容性

- 默认 File/JSON、middleware mode 行为不变；
- tool mode CRUD 使用同 scope 和 repository；
- Gateway get/import/export/clear 不绕过事务；
- summarization emergency 路径与普通写路径一致；
- noop backend 同步契约；
- core 新代码无 `deerflow.*` import；
- storage_class 错误 fail-fast，不静默回退。

### 15.6 性能基线

至少测试每 scope 100、1,000、10,000 facts：

- 单 fact 写入 P50/P95；
- FTS top-k P50/P95；
- startup 与 migration 时间；
- Markdown 全量扫描和 manifest 校验时间；
- JSON/Markdown/journal 磁盘占用；
- 多 worker 写冲突率和重试次数。

## 16. 迁移与发布

1. 先发布只读识别与 dry-run 工具；
2. 输出源文件、目标 DB、scope 数、fact 数、hash 对账报告；
3. 迁移前备份原 `memory.json`；
4. 生成逐 fact Markdown 和 v2 manifest 后做逐 scope/hash 校验；
5. 切换 backend 前停止/排空旧写队列；
6. 切换后保留 JSON 只读备份，不双写为两个事实源；
7. 支持显式 rollback 到备份，但回滚期间产生的新写必须有处理方案；
8. 首次接入 retrieval 执行 `rebuild_index()`；
9. 状态 API 暴露 schema、backend、migration、index backlog 和 degraded 状态。

## 17. 风险与裁决

| 风险/冲突 | 裁决 |
|---|---|
| project 加入 scope | 本轮撤回；后续独立修改 ABC/runtime/retrieval/injection |
| embedding 存放 | storage 拥有 sidecar；不入 fact/Markdown |
| JSON 与 Markdown 双主 | 禁止；Markdown 保存完整 fact，JSON 只保存 manifest/summaries |
| 原子写被误认为并发事务 | File 使用 scope lock + revision + journal；明确只承诺单机本地文件系统 |
| storage API 继续整文档 | 保留兼容层，同时新增 fact repository 和 `apply_changes` |
| 每次读取更新 lastAccessed 导致写放大 | 第一版不逐次同步写；使用异步聚合或先仅用 updatedAt |
| retrieval 网络调用阻塞文件锁 | 所有 retrieval 调用在提交和释放文件锁之后进行，失败进入重试/degraded |
| File fallback 隐藏检索失效 | capability/status 显式暴露，禁止无提示空结果 |
| backend 配错 | 持久化类解析 fail-fast；模型客户端才允许优雅降级 |

## 18. 交付物

1. S1/S2/S3/S6 最终契约文档；
2. fact schema v2 JSON Schema/Pydantic 等价定义；
3. `MemoryScope` 与 scope 分层说明；
4. File backend 加固与兼容测试；
5. 单 fact Markdown parser/renderer、manifest 和 journal/recovery；
6. 旧 JSON→Markdown+manifest 迁移/验证/回滚工具；
7. retrieval adapter、rebuild/status；
8. 并发、安全、迁移、索引和端到端测试；
9. README、相关 AGENTS.md、config.example.yaml 更新。

## 19. 已确认决策与剩余联调项

### 19.1 已确认

- [x] project scope 从本轮撤回，后续独立 PR；
- [x] thread_id 只作来源；
- [x] 接受 fact schema v2 整体草案；
- [x] category 使用核心枚举 + 扩展类别；
- [x] 删除为物理删除；
- [x] 本轮使用 File/JSON，不实现 SQLite；
- [x] 单 fact Markdown 是 canonical fact storage，主 JSON 是 manifest/summaries；
- [x] 检索算法归 retrieval 模块，storage 调用其 API；

### 19.2 编码前仍需与 retrieval 负责人锁定

- [ ] retrieval port 的 `upsert/remove/search/rebuild/status` 准确签名；
- [ ] 同步调用还是异步 job，以及超时和重试责任；
- [ ] SearchResult 的字段、score 语义和错误类型；
- [ ] retrieval 是否直接读取 Markdown 路径，还是接收解析后的 fact；
- [ ] 物理删除时 retrieval remove 失败的补偿与重建策略；
- [ ] scope 过滤由 storage 预筛还是 retrieval 强制执行。安全要求是 retrieval 必须再次强制校验 scope。

这些联调项锁定后再开始 S6 编码，避免 storage 与 retrieval 各自假设不同接口。

## 20. SQLite 暂缓决策说明

### 20.1 使用复杂度

SQLite 对使用者并不复杂：Python 自带 `sqlite3`，部署通常只增加一个数据库文件，不需要安装独立服务。真正的复杂度主要在开发侧：表结构和 migration、事务边界、WAL/锁重试、备份恢复，以及把当前整文档逻辑重构为行级 CRUD。

### 20.2 当前是否必要

对本轮目标不是硬性必要。若满足以下条件，File/JSON + 单 fact Markdown 可以先交付：

- 单机本地文件系统；
- 写入频率较低；
- worker 数量有限；
- 实现了 scope file lock、revision、journal 和恢复测试；
- retrieval 模块可以独立建立索引；
- 团队接受“大量小文件”和多文件提交协议的维护成本。

### 20.3 何时应重新评估

出现任一情况时，应重新评估 SQLite/Postgres：

- 多 worker 下锁等待、revision conflict 或恢复事件频繁；
- 每个 scope 达到数万 facts，大量小文件扫描明显变慢；
- 需要可靠的持久化队列、复杂过滤、分页或审计查询；
- consolidation/clear 等多文件操作越来越复杂；
- 需要多机器部署（此时更应直接评估 Postgres，而不是共享 SQLite 文件）。

因此当前裁决是：**本轮不实现 SQLite，但 repository、scope 和 retrieval port 不应绑定 Markdown 文件细节**。这样未来若数据量或并发压力证明 File backend 不够用，可以替换 backend，而不重写 updater、retrieval 和 injection。
