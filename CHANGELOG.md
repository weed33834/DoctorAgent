# Changelog

本文件记录 DoctorAgent 所有版本的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

---

## [0.3.1] - 2026-07-31

### 修复

#### 临床链路缺口
- 修复 CDS Hooks `doctoragent-patient-view` 服务预取模板遗漏 `observations` 的问题：原模板仅请求 patient / medications / allergies / conditions，导致符合 CDS Hooks 规范的 EHR 永远不会发送 Observations，`_extract_vitals_labs` 收到空列表，确定性规则引擎无法评估生命体征与检验值。新增 `Observation?patient={{context.patientId}}&category=vital-signs,laboratory` 预取模板，使 patient-view 的 "vitals / labs" 评估承诺真正可达。

### 重构

#### 去除冗余代码与自造轮子（统一使用已有专业依赖）
- **统一 `_extract_json`**：移除 `self_evolution.py` / `dynamic_tools.py` / `tree_of_thought.py` 中三份逐字拷贝的 `_extract_json`，以及 `clinical_tools.py` 中功能更弱的 `_parse_json_lenient`，统一引用 `model.agent._extract_json`（此前已有 4 个文件采用此模式）。
- **统一 FHIR Bundle 解析**：将 `fhir/client.py` 的 `_extract_bundle_entries` 与 `cds_hooks/service.py` 的 `_bundle_entries` 两处重复实现收敛为 `fhir/parser.py` 的公共 `extract_bundle_entries`；同时移除 `client.py` 中的 `_coding_display_proxy` 本地副本，直接复用 `parser.coding_display`。
- **统一鉴权原语**：将 `cds_hooks/router.py`、`advanced_routes.py`、`server.py` 中内联的 `hmac.compare_digest` 与直接读取 `os.environ` 的鉴权逻辑收敛到 `api/auth/_guards.py` 的 `resolve_token` / `oidc_is_configured` / `verify_bearer`（顺带激活了原本为死代码的 `verify_bearer`），移除 3 处 `import hmac` / `import os`。
- **统一向量运算**：`vectorstore/sqlite_store.py` 手写的逐行余弦相似度循环替换为 `_utils.cosine_similarity_matrix`（numpy 批量计算，自动处理维度不匹配与零模行）。
- **统一 async-to-sync**：`clinical/evaluation/benchmark.py` 手写的 `ThreadPoolExecutor + asyncio.run` 桥接替换为 `_utils.async_to_sync`。
- **统一 SQLite 连接**：`sync/governance.py` 与 `model/cost_tracker.py` 手写的 `sqlite3.connect + PRAGMA WAL/busy_timeout` 替换为 `_utils.open_sqlite`（文件库路径）。

### 验证

#### 端到端链路连通性
- 新增 8 大链路、39 项检查的端到端冒烟验证（验证后已移除临时脚本）：临床规则引擎、`run_clinical_workflow`（无 LLM 降级路径）、FHIR Bundle → patient_context、CDS Hooks 服务调用、Corrective RAG、审计日志 + 事件广播（含 PHI 脱敏校验）、API 鉴权 + `/clinical/analyze` + CDS 发现、Guardrail。全部通过。
- 全量单元测试 2314 passed / 2 skipped。

### 文档
- 同步更新 README（中/英/日）：测试徽章更新为 2314 passed；API 表补全 `/clinical/analyze`、`/cds-services`、`/vault/ask/stream`、`/events`、`/ws`、`/mcp` 等端点；中文与日文 README 由旧版"文档库优先"重写为与英文版一致的"临床 AI 平台优先"结构，补全临床能力、CDS Hooks、OIDC 认证、镜像仓库等缺失章节。
- 修正 `docs/CLINICAL_CAPABILITIES.md` §6 过时的依赖声明：clinical extra 实际包含 `fhir.resources` / `instructor` / `openai` / `langgraph` / `authlib`，原"仅新增 `fhir.resources>=8.0`"的描述已更新。
- `docs/MEDICAL_PIVOT_DESIGN.md` 标注为历史设计文档（权威说明以 CLINICAL_CAPABILITIES.md 为准），并修正设计稿中 `medkit` 依赖的过时声明（实际改用 httpx 直连官方 API）。
- 修正 `docker-compose.yml` 中"FhirConfig 未接入 config.py"的过时注释：`ClinicalConfig` 已在 `config.py` 接线，`DOCTORAGENT_CLINICAL__FHIR_BASE_URL` 现通过环境变量注入并支持 HAPI FHIR live 读取。
- 统一仓库与镜像 URL：README 测试徽章、`git clone` 示例、Dockerfile OCI `image.source` 标签、`server.py` OpenAPI contact、`tray.py` 文档跳转、CHANGELOG 发布链接、Makefile/RELEASE.md 动作 URL 全部对齐到权威主仓库 `github.com/weed33834/DoctorAgent`；GHCR 镜像路径对齐到 `ghcr.io/weed33834/doctoragent`（跟随 `${{ github.repository }}` 小写）。
- 修正 `server.py` OpenAPI `license_info` 错误标注为 Apache 2.0 的问题（项目实际为 MIT）。
- `RELEASE.md` 将写死的 `0.3.0` 示例改为 `<VERSION>` 占位符，避免每次发版失真；移除与 `make check-version` 重复的手动 grep 校验段。
- `README.md` 合并冗余的 "Repository" 与 "Mirrors / 镜像" 章节。
- `AGENTS.md` "Future Enhancements" 移除已落地的并行工具执行 / 流式响应 / 多智能体协作等条目。
- `docs/OPERATIONS.md` 修正指标名拼写 `ftx_index_lag_seconds` → `fts_index_lag_seconds`（与 FTS5 命名一致）。

---

## [0.3.0] - 2026-07-31

### 新增

#### 临床安全与自进化
- 新增 RxNorm 药物等价识别：基于 RxCUI 的重复用药检测，覆盖品牌药/通用名/复方制剂等价类
- 接入自进化引擎（self-evolution engine）到临床编排器，实现基于反馈的持续改进闭环
- 安全规则引擎扩展重复用药检测维度（RxCUI 等价 + 通用名匹配）

#### 容错与可观测
- 新增 per-tool 熔断器配置：每个工具可独立配置失败阈值/恢复策略，避免单工具故障级联
- 熔断器支持半开（half-open）状态探测，自动恢复已恢复的工具

#### 企业运维文档
- 新增 `docs/OPERATIONS.md`：企业运维手册（部署、监控、备份、扩缩容）
- 新增 `docs/CLINICAL_USER_GUIDE.md`：临床用户操作手册
- 新增 `docs/DISASTER_RECOVERY.md`：灾备恢复方案（RPO/RTO、故障切换、数据恢复）
- 新增 `docs/UPGRADE_ROLLBACK.md`：升级与回滚方案（蓝绿/灰度、版本兼容矩阵）

#### 质量保障
- CI 新增覆盖率门槛（`--cov-fail-under=60`）：守住临床/安全/Agent/API 核心模块质量基线

### 变更

#### 依赖升级（放开上界，兼容最新主版本）
- `openai`：`<2.0` → `>=2.0,<3.0` + `instructor`：`>=1.0` → `>=1.13`（openai 2.x 与 instructor 1.13+ 跨主版本耦合，统一升级到现代栈；已通过临床结构化输出/Agent 全量测试验证）
- `langgraph`：`<1.0` → `<2.0`（兼容 langgraph 1.x 声明式 StateGraph）
- `starlette`：`<1.0` → `<2.0`（兼容 starlette 1.x）
- `chromadb`：`<1.0` → `<2.0`（兼容 chromadb 1.x 向量库）
- `mcp`：`<2.0` → `<3.0`（兼容 MCP 2.x 协议）
- `deepeval`：`<4.0` → `<5.0`（兼容 DeepEval 4.x 评估库）
- `datasets`：`<3.0` → `<6.0`（兼容 HuggingFace datasets 5.x）
- `google-cloud-kms`：`<3.0` → `<4.0`（兼容 GCP KMS 3.x）
- `structlog`：`<26.0` → `<27.0`
- `pytest`：`<9.0` → `<10.0`、`pytest-cov`：`<7.0` → `<8.0`（dev）

> 所有放开上界的依赖在代码中均通过核心稳定 API 或 try/except 优雅降级使用，
> 新主版本不影响最小安装与默认运行路径。Dockerfile wheel 构建同步更新
> `structlog`/`openai`/`instructor` 约束。

### 修复
- 修复 `test_agent_mcp.py` 对旧版 MCP `Server.server_info` 的引用（mcp 1.x 改用 `Server.name`）

### 变更（版本）
- 版本号从 0.2.0 升级到 0.3.0

---

## [0.2.0] - 2025-07-31

### 新增

#### 前端控制台（重大更新）
- 新增 8 个智能体高级功能模块前端界面：记忆管理、生命周期钩子、可观测性、强化学习反馈、多智能体协作、插件管理、A/B 实验、Prompt 模板
- 新增 Command Palette 命令面板（Ctrl/Cmd+K 快速搜索页面和命令）
- 新增键盘快捷键：Alt+1-9 切换页面、Ctrl+Enter 发送消息、? 打开帮助
- 新增使用文档中心（13 篇文档，含快速入门、功能教程、FAQ、配置指南）
- 新增功能介绍 Tooltip 系统（专业术语 ℹ 图标悬停解释）
- 新增首次使用引导（5 步 Onboarding Tour）
- 新增 12+ 动画效果：成功彩带、心跳加载器、闪光加载条、脉冲点、卡片弹跳、错误抖动、成功对勾、渐变流动等

#### 部署与发布
- 新增 `.github/workflows/release.yml`：三平台统一发布工作流（PyPI + Docker GHCR + GitHub Release）
- Dockerfile 新增 OCI 标准镜像标签（版本号、许可证、源码链接）
- docker-compose.yml 新增 GHCR 预构建镜像选项

### 修复

#### 高严重度
- 修复文件上传二进制文件被当文本处理的问题（改用 `/inbox/submit/batch` multipart 接口）
- 修复并发流损坏对话状态的问题（流式生成中拒绝发送新消息）
- 修复 localStorage 配额超限崩溃（自动裁剪旧会话消息重试）
- 修复 TDZ 错误（`Cannot access 'chatState' before initialization`）导致 IIFE 中断

#### 中严重度
- 修复文件上传失败后 fileNote 误导用户的问题
- 修复 Tab 切换时流式响应未中止的问题
- 新增全局错误兜底（`unhandledrejection` + `window.onerror`）
- 401/403 鉴权失败新增 Token 输入框红色高亮 + "去配置"按钮
- 新增文件大小限制（20MB）和上传中 loading 指示

#### 低严重度
- 修复 `parseInt/parseFloat` 用 `||` 兜底导致 0 值被误判的问题
- 健康检查定时器按 `visibilitychange` 暂停/恢复
- 新增 `online/offline` 网络状态监听
- 修复 README/CONTRIBUTING 中引用不存在的 `ollama`、`keyring` extras

### 变更
- 版本号从 0.1.0 升级到 0.2.0
- Development Status 从 `3 - Alpha` 升级到 `4 - Beta`

---

## [0.1.0] - 2025-01-15

### 初始发布
- 核心 Agent 引擎：ReAct 循环、工具调用、流式响应
- 临床工作台：CDS Hooks 2.0、FHIR R4、SNOMED CT/LOINC/ICD-10-CM 术语绑定
- PHI 脱敏：三种策略（redact/pseudonymize/mask）
- 确定性安全规则引擎：生命体征危急值、检验异常、药物相互作用
- LangGraph 声明式 DAG 智能体编排
- 加密 Vault（AES-256-GCM + Argon2id 密钥派生）
- HMAC-SHA256 审计链
- 多租户支持
- FastAPI 服务端 + 前端控制台
- Docker 多阶段构建
- GitHub Actions CI/CD（测试 + 安全扫描）

---

[0.3.1]: https://github.com/weed33834/DoctorAgent/releases/tag/v0.3.1
[0.3.0]: https://github.com/weed33834/DoctorAgent/releases/tag/v0.3.0
[0.2.0]: https://github.com/weed33834/DoctorAgent/releases/tag/v0.2.0
[0.1.0]: https://github.com/weed33834/DoctorAgent/releases/tag/v0.1.0
