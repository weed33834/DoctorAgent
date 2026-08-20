# DoctorAgent 优化报告

> 日期：2026-08-19
> 基线：v0.3.3 · 212 个 Python 模块 · 79,679 行代码 · 112 个测试文件

---

## 一、体检概览

| 维度 | 状态 | 说明 |
|------|------|------|
| Ruff lint | ✅ 全过 | 零告警 |
| 测试 | ⚠️ 240 pass / 1 fail | 失败项为缺 `fhir.resources` 依赖，非代码问题 |
| 代码规模 | 79,679 行 / 212 模块 | 生产级规模 |
| Dockerfile | ⚠️ 2 处问题 | 许可证标签错误 + 健康检查低效 |
| 安全模式 | ✅ 无高危 | 无 `shell=True`、无 `pickle.load`、无 `hashlib.md5`、无硬编码密钥 |

---

## 二、已修复问题（7 项）

### 🔴 P0 — 功能性 Bug

#### 1. `recall_facts` 忽略 query 参数

- **文件：** `doctoragent/model/rag.py:545`
- **问题：** `recall_facts(query, limit)` 接受 `query` 参数但从未用于过滤，只按重要性返回 top-N。导致 RAG 长期记忆召回与查询无关——用户问"阿司匹林"可能返回"用户喜欢简洁回答"这类不相关记忆。
- **修复：** 先用 `LIKE` 做 query 相关过滤，结果不足时 fallback 到按重要性返回（兼容跨语言/同义表述场景）。双策略保证不会比原来更差。
- **影响范围：** `ContextEngineer._build_memory_context()` → 所有使用记忆系统的对话

#### 2. 系统提示词错别字

- **文件：** `doctoragent/model/rag.py:1174`
- **问题：** `SYSTEM_PROMPT_WITH_MEMORY` 中 "AI 助力" 应为 "AI 助手"
- **修复：** 已修正。该提示词会进入每次带记忆的对话，影响 AI 自我角色定位。

### 🟡 P1 — 性能问题

#### 3. `recall_episodes` 全表加载无 LIMIT

- **文件：** `doctoragent/model/rag.py:711`
- **问题：** `recall_episodes` 从 `memory_episodic` 表加载该租户**全部** episode 行到内存中做 Python 侧排序。大租户积累数千条 episode 时会导致内存飙升和延迟增加。
- **修复：** 添加 `_EPISODE_RECALL_SCAN_LIMIT = 200` 安全上限（10× 典型 `limit` 值，不影响召回质量）。SQL 层面只取最近 200 条做精细排序。

#### 4. SQLite 记忆表无索引覆盖

- **文件：** `doctoragent/model/rag.py:447` (`_create_indexes` 新方法)
- **问题：** `memory_long_term`、`memory_episodic`、`conversation_turns` 三张表均无索引。所有 `WHERE tenant_id = ? ORDER BY ...` 查询走全表扫描。
- **修复：** 添加 4 个覆盖索引：
  - `idx_memory_lt_tenant_imp` — 长期记忆按租户+重要性查询
  - `idx_memory_ep_tenant_ts` — 情景记忆按租户+时间查询
  - `idx_memory_ep_consolidated` — 记忆整合扫描
  - `idx_conv_turns_session_ts` — 对话历史按会话查询
- **幂等性：** 使用 `CREATE INDEX IF NOT EXISTS`，每次 init 安全调用，老库自动升级。

### 🟢 P2 — 代码质量

#### 5. Dockerfile 许可证标签错误

- **文件：** `Dockerfile:106`
- **问题：** OCI 标签声明 `licenses="MIT"`，但项目实际使用 Apache-2.0（README 明确声明）
- **修复：** 改为 `Apache-2.0`

#### 6. Dockerfile 健康检查低效

- **文件：** `Dockerfile:109-110`
- **问题：** 健康检查用 `python -c "import urllib.request; ..."` 启动一个 Python 解释器做 HTTP 请求，每次检查多开销 ~200ms 解释器启动时间
- **修复：** 改用 `curl -fsS`，镜像中已安装 curl，更快更轻

#### 7. 重复 `import re`

- **文件：** `doctoragent/model/rag.py:124`
- **问题：** `_split_fact_candidates` 函数内部 `import re`，但模块顶层第 54 行已导入。局部重复导入虽不影响功能，但增加函数调用开销（每次执行都走一次 import 机制检查）
- **修复：** 移除局部导入，保留注释说明

---

## 三、改动统计

```
 Dockerfile               |   4 +--
 doctoragent/model/rag.py | 122 +++++++++++++++++++++++++++++++++++++++++------
 2 files changed, 110 insertions(+), 16 deletions(-)
```

---

## 四、验证结果

| 检查项 | 结果 |
|--------|------|
| Ruff lint | ✅ All checks passed |
| Ruff format | ✅ 1 file already formatted |
| RAG 测试 (15 项) | ✅ 15 passed |
| 核心模块测试 (110 项) | ✅ 110 passed |

---

## 五、建议后续优化（未实施，需评估）

| 优先级 | 项目 | 说明 |
|--------|------|------|
| P1 | 拆分大文件 | `advanced_routes.py` (4447行)、`server.py` (3714行)、`rag.py` (3658行) 应按职责拆分 |
| P2 | 减少广谱异常捕获 | 全项目 402 处 `# noqa: BLE001`，建议按场景收敛为具体异常类型 |
| P2 | SQLite → async | 当前在 async FastAPI 上下文中使用同步 sqlite3，高并发时阻塞事件循环 |
| P2 | 语义 recall for facts | 当前 `recall_facts` 用 `LIKE` 做文本匹配，建议为 facts 也增加 embedding 支持（episodes 已有） |
| P3 | 依赖版本固定 | pyproject.toml 中部分依赖范围较宽（如 `numpy>=1.26,<3.0`），生产环境建议 lock 到具体版本 |
| P3 | 测试覆盖率 | 当前 CI 门禁 60%，核心模块（clinical/agent/api）建议提升到 80%+ |
