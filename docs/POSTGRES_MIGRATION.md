# Postgres 迁移与 RLS 设计（立项文档）

> 目标：把存储层从 SQLite 迁移到 PostgreSQL，并用**数据库级行安全（RLS）**
> 替换当前"每个查询手写 `AND tenant_id=?`"的隔离方式——消除漏写一行即跨租户
> 泄露病历的结构性风险。
>
> 本文是实施蓝图，不是已落地功能。当前状态见 CHANGELOG 与 INTEGRATIONS.md。

---

## 一、现状盘点

| 存储 | 文件 | 说明 |
|---|---|---|
| 任务/chunk | `orchestration/task_store.py` | vault_chunks、FTS5(BM25)、任务状态机 |
| 记忆/对话历史 | `model/rag.py` MemorySystem | 长期记忆、情景记忆、对话轮次 |
| 会话 | `conversations.py` | conv_* 三表（v0.3.10 起带 tenant_id） |
| 向量(内联) | rag.py 稠密路径 | struct-pack BLOB + numpy 余弦 |
| 外部向量 | vectorstore/* | 已支持 Chroma（v0.3.18 接线） |

所有访问均为同步 `sqlite3` 直连；租户过滤靠约 90+ 处手动 WHERE 子句。

## 二、目标架构

```
FastAPI ──► SQLAlchemy 2.0 (async) ──► asyncpg ──► PostgreSQL 15+
                                        │
                                        ├─ pgvector：替代内联稠密路径
                                        ├─ tsvector/jieba 分词：替代 FTS5
                                        └─ RLS Policy：数据库级租户隔离
```

### 连接与租户上下文

```python
# 每请求设置一个事务级 GUC，RLS 策略读取它
SET LOCAL app.tenant_id = :tenant_id;
```

应用侧在 auth 依赖中解析租户（复用 v0.3.10 的 `_tenant(request)`），
由连接池的 `connect` 钩子执行 `SET LOCAL`。

### RLS 策略模板（每张业务表）

```sql
ALTER TABLE vault_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE vault_chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON vault_chunks
    USING      (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
```

要点：
* `FORCE` 使表属主也受约束（除显式 `BYPASSRLS` 的迁移角色）；
* 应用连库角色**不授予** BYPASSRLS；仅迁移工具用独立高权角色；
* 现有 90+ 处手动 WHERE **保留**作为纵深防御，RLS 是兜底而非替代。

## 三、分阶段实施

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 | SQLAlchemy async + asyncpg 双驱动抽象（`doctoragent/db/`） | ✅ v0.3.22 |
| P2 | conversations ORM 试点 + 双跑门面（`AsyncConversationRepository` / `ConversationFacade`） | ✅ v0.3.23 |
| P3a | 真机驱动验证（PG16+pgvector 容器）：asyncpg 往返、tenant_scope GUC 绑定、**RLS 端到端实证**（跨租户不可见/无 GUC 零行/WITH CHECK 拒绝/跨租户 UPDATE 0 行）、仓储 parity、pgvector `<=>` 冒烟 | ✅ v0.3.24（`tests/test_pg_integration.py`，env 门控） |
| P3b | task_store/memory 铺开 ORM；pgvector 替代内联稠密路径 | 进行中 |
| P4 | 全端点跨租户渗透测试套件（RLS 已单点实证，需铺开覆盖） | 待 P3b |
| P5 | compose profile（`deploy/docker-compose.postgres.yml` ✅）+ 迁移/回滚 CLI | 部分 |

**关键实证（v0.3.24，真机 PG16+pgvector）**：RLS 策略在非超管角色下完全生效——
A/B 两租户互相不可见、未绑定 GUC 的会话零行、WITH CHECK 拒绝错租户插入、
跨租户 UPDATE 影响 0 行。注意：应用连接角色必须是 NOSUPERUSER（超管绕过
RLS，容器镜像默认角色即超管，部署时必须换专用低权角色）。

## 四、风险清单

* jieba 中文分词在 PG 侧无等价物 → 方案：应用侧分词后写入 tsvector，或读路径继续用 SQLite BM25 只读副本；
* 同步代码大面积 await 化 → 借助 `asyncio.to_thread` 过渡包装，避免一次性重写；
* 现有部署的数据迁移 → 提供 `doctoragent migrate-sqlite-to-pg` CLI（按租户批量导出导入，含审计链校验）。

## 五、明确不做

* 不做 MySQL/其他数据库方言——医疗自托管场景只承诺 PG 一条线；
* 不在迁移完成前宣传"数据库级隔离"字样（README 合规表维持现状措辞）。
