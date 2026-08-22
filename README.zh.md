# DoctorAgent（医师智能体）

> 临床 AI 医生智能体 · Clinical AI agent for doctors
>
> 开源自托管的临床决策支持（CDS）智能体：确定性用药相互作用 / 危急值安全规则 + 多智能体 LLM 推理，支持 FHIR R4、CDS Hooks 2.0、SMART-on-FHIR、PHI 脱敏、RAG 文档库、HIPAA 审计链。

[English](README.md) · [日本語](README.ja.md)
<p align="center"><img src="assets/logo_256.png" alt="DoctorAgent" width="120"/></p>


**关键词**：临床AI、医疗AI、医疗大模型、临床决策支持、CDS Hooks、FHIR、药物相互作用检测、危急值预警、PHI脱敏、RAG检索、文档库、LangGraph多智能体、LLM智能体、FastAPI、Python、HIPAA、患者安全、私有化部署、本地部署、离线运行

---

## 目录

- [这是什么](#这是什么)
- [演示](#演示)
- [为什么要做这个](#为什么要做这个)
- [一段话说架构](#一段话说架构)
- [快速开始](#快速开始)
- [你可以在它上面做什么](#你可以在它上面做什么)
- [商业使用](#商业使用)
- [合规现状](#合规现状当前)
- [测试姿态](#测试姿态)
- [常见问题](#常见问题)
- [相关项目](#相关项目)
- [许可证](#许可证)

---

## 这是什么

DoctorAgent 把"确定性安全规则"和"多智能体 LLM 推理"结合在一起，帮临床医生处理用药审查、文献检索、PHI 处理和病历文书。可以本地部署，可以接入本地 Ollama，也可以用云端 LLM。

跟 GitHub 上另外 47 个"医疗 AI"项目相比，我们做对了三件事：

1. **安全规则完全离线运行**。五项确定性检查（危急值/检验/DDI/过敏/重复用药）在本地执行，不调任何 LLM。当规则引擎和 LLM 意见冲突时，规则引擎赢——永远。
2. **数据留在你的硬件上**。文档库 AES-256-GCM 加密存储（密钥 PBKDF2-SHA256 / Argon2id 派生），审计日志 HMAC-SHA256 签名链式防篡改。可以完全断网运行。
3. **标准集成是真接通的，不是 PPT 上的 roadmap**。CDS Hooks 2.0 端点、FHIR R4 资源处理器、SMART-on-FHIR 认证、SNOMED CT / LOINC / ICD-10-CM 术语绑定都在代码路径里，不是写在幻灯片上的承诺。

当前版本 v0.3.3，Beta。我们不装：没有 FDA 510(k)，没有 HIPAA 认证，没有等保三级。这些都写在了 roadmap 里，让你做规划的时候不被坑。

---

## 演示

[看 40 秒走查 →](assets/demo/demo.mp4)

![DoctorAgent 宣传封面](assets/demo/doctoragent_cover.png)

**真实模型宣传片**（step-3.5-flash 真实多轮临床对话 + 全模块巡览，已剪辑去冗）：
[▶ 播放 `assets/demo/doctoragent_demo_edit.mp4`](assets/demo/doctoragent_demo_edit.mp4)

- 完整走查视频：`assets/demo/doctoragent_live_demo.mp4`
- 全模块截图：`assets/demo/final/`（对话/临床/安全/PHI/Vault/高级RAG/编排/系统/企业/记忆/评估/亮色）
- 真实对话内容：`assets/demo/final/real_chat_content.json`（华法林/肝硬化/剂量计算）
- 宣传用主图：`assets/demo/doctoragent_cover.png`

**控制台全流程走查**（引导页 → 多轮医疗对话 → 角色切换 → 全模块巡览 → 主题切换）：
[`assets/demo/demo_console.mp4`](assets/demo/demo_console.mp4) · 全模块截图见 `assets/demo/`（00 引导 / 01-02 对话 / 03-09 临床·安全·PHI·Vault·编排·系统·企业 / 20-21 会话）。

或者看下面这些截图——它们来自真实控制台（本地 SQLite + Ollama 跑出来的），README 里没有 P 图。

| 临床工作台 | 安全规则引擎 |
|:---:|:---:|
| ![临床工作台：生命体征、过敏、用药](assets/screenshots/02-clinical.png) | ![确定性安全规则：5 大类 + 严重度标签](assets/screenshots/03-safety-rules.png) |

| PHI 脱敏 | 多租户管理 |
|:---:|:---:|
| ![PHI 脱敏：原文/高亮/脱敏对比 + 类型分布](assets/screenshots/04-phi-deidentify.png) | ![租户卡片：隔离提供商、活跃账户](assets/screenshots/06-tenants.png) |

| 系统仪表板 |
|:---:|
| ![系统状态：运行态、模型配置、资源池](assets/screenshots/05-system-status.png) |

---

## 功能全景

一套完整、可生产的智能体平台——**绝大多数操作都能在对话里完成**：

- **对话即操作**：对话里切临床科室、建/导知识库、导资料、查系统状态、改提示词/加专家/加技能（与后台/管理界面同库互通）
- **通用智能体工具**：联网搜索、网页抓取、当前时间、安全数学计算(simpleeval)、沙箱跑 Python 并出图
- **服务端会话**：持久化、全局内容搜索、分叉、赞踩反馈、分享链接(公开查看+撤销)、自动命名、摘要
- **专科医生角色**：18 个内置医生人设（心内/外科/麻醉/急诊/ICU/儿科/…），含专科提示词、红旗信号、默认工具，控制台顶栏可切换
- **内置医学知识库**：12 篇必备文档（危急值/药物相互作用/参考范围/常见急症…）启动自动播种；附资料清单 + 导入引导可扩展
- **记忆**：短期/情景/语义/程序性 + 整合压实与遗忘
- **RAG 知识库**：PDF/DOCX/XLSX/MD 导入、混合检索、引用溯源
- **多智能体与工具**：ReAct 循环、代码沙箱、浏览器自动化、群聊/辩论、MCP 客户端+服务端、A2A 协议
- **平台**：RBAC+OIDC+MFA(TOTP)、多租户组织、审计链、AI 安全红队、互操作、容灾、成本/计费、可观测、错误码体系

详见临床资料引导（`docs/KNOWLEDGE_CATALOG.md`、`docs/KNOWLEDGE_UPLOAD_GUIDE.md`）。

---

## 为什么要做这个

LLM 医疗助手到处都是。绝大多数：

- 提示词稍微偏出分布就胡编药物剂量。
- 默认把病历发到第三方 API，没开关。
- 关掉"创造性模式"那一刻安全推理就没了。
- 审计只有一个点（prompt 日志），有 shell 的人能改写。

我们要的是：

- 临床医生点一个按钮就能查到"模型有没有为这个病人查 DDI"，并读到一条不可篡改的记录。
- IT 管理员把系统指向本地 Ollama，关掉所有外网流量。
- 合规官能证明某天某次就诊里模型看到了什么、做了什么决定。

这些都不是黑科技。都是些无趣的纪律——FHIR 资源、HMAC 链、审计日志、RBAC。我们做出来了，因为试过的替代品都在审计场景里崩了。

---

## 一段话说架构

FastAPI 服务同时承载 API 和静态控制台。控制台是 **Vue 3 + Vite + Tailwind** 单页应用（源码在 `frontend/`，构建产物在 `doctoragent/api/static/console/dist`），覆盖 **38 个视图**：智能对话、临床工作台、PHI 工具、Vault、RAG、知识图谱、记忆、Prompt 模板、DAG、评估、自进化、强化学习、多智能体协作、知识库、数据管道、数据治理、多模态、互操作、灾难恢复、成本/定价、沙箱、安全扫描/红队、配置、连接、租户、系统状态、审计、合规、运维、设置、钩子、可观测性、插件、A/B 实验，加上视图切换。控制台是**唯一** UI，无桌面 GUI。入站文档走：分类器 → AES-256-GCM 加密存储 → SQLite FTS5 索引 + 向量索引 → 检索管道（HyDE + RRF + 交叉编码重排）→ LLM 智能体（病史 → 用药 → 文献 → 文书的固定 DAG）。每一步都写入 HMAC 链式审计。

固定 DAG 的智能体设计对我们不可妥协——临床工作流一旦编译完成，LLM 不能自己绕开某个安全步骤。这点大多数"智能体框架"故意允许，而我们在临床管道里明确禁止。

---

## 快速开始

需要 Python 3.10+，约 200MB 磁盘。

```bash
git clone https://github.com/weed33834/DoctorAgent.git
cd DoctorAgent
pip install -e ".[server]"
# 构建 Web 控制台（Vue 3 + Vite）。仓库已含预构建 dist，仅改前端时需要：
cd frontend && npm install && npm run build && cd ..
bash start.sh
```

浏览器打开 <http://127.0.0.1:8000/console/>。安全规则引擎和文档库**完全离线可用**，不需要 LLM 密钥。接入 Ollama 或云端 LLM：控制台 → "连接" 配置后即可解锁智能体功能。

### 验证你拿到的不是损坏版本

```bash
pytest tests/ -q
```

期望 `2314+ passed`。如果数字掉了，说明哪里坏了。别发那个版本。

---

## 你可以在它上面做什么

代码 Apache-2.0 许可，明确设计为可扩展。钩子系统有 15 个触发点（request_received / before_tool_call / after_llm_response / before_audit_write 等），可以挂载 Python 脚本。插件管理器在启动时注册额外入口点。

### 企业级与平台能力

DoctorAgent 在核心临床能力之外，已落地一整套**可上线、可信、可观测、可企业化**的平台能力：

- **A2A 跨 Agent 协议**（`doctoragent/a2a/`）：`/.well-known/agent.json` + `/a2a/rpc`，可与其它 Agent 框架互操作
- **MCP 客户端**：连接外部 MCP 服务器并导入工具（`POST /mcp/connect`）
- **企业级平台**（`doctoragent/enterprise/`）：组织/部门树、用户生命周期、TOTP MFA 双因子、账号锁定、预算阶梯超限、配额、公告、维护模式、API Key、审计导出
- **数据治理目录**（`doctoragent/governance/`）：数据资产/血缘/质量/敏感度分类（含 PHI）
- **语义缓存**（`model/semantic_cache.py`）：按查询语义命中缓存，降低 TTFT 与成本
- **模型比价/成本看板**（`model/pricing.py` + `/api/v1/cost/*`）
- **语音对话链路**（`doctoragent/voice/`）：ASR + TTS，控制台语音输入/朗读
- **浏览器自动化 / 多框架适配器 / 群聊编排**（`tools/browser_tool.py`、`agent/adapters.py`、`orchestration/group_chat.py`）
- **生产部署资产**（`deploy/k8s/`、`deploy/grafana/`）+ 质量门禁（`scripts/eval_gate.py`、`scripts/security_smoke.py`）
- **AI 安全威胁库+红队**（`security/threat.py`）：威胁用例/注入检测/安全事件/红队演练
- **Agent 互操作**（`interop/`）：外部 Agent 目录/信任等级/互操作策略/A2A 任务监控
- **容灾与连续性**（`disaster/`）：备份任务/DR 计划/演练(实测 RTO-RPO)/故障注入/连续性看板
- **多模态资产库**（`multimodal/`）+ **数据管道**（`datapipeline/`）+ **知识库管理**（`knowledge_base.py`）+ **任务中心**（`taskcenter.py`）+ **用量分析**（`/analytics/overview`）


### 临床专业知识库
智能体出厂内置 12 篇**基础医学知识**（危急值/药物相互作用/正常范围/常见急症处理），并预置 **18 个专科医生角色**（心内/外科/麻醉/急诊/ICU/儿科…，可在控制台顶栏切换）。如需更强的专科能力，请按《医学资料准备清单》把 PDF 等医学资料导入知识库：
- 资料清单：`docs/KNOWLEDGE_CATALOG.md`
- 导入操作引导：`docs/KNOWLEDGE_UPLOAD_GUIDE.md`（控制台上传 / CLI `import` / Inbox 目录三种方式）
- 便捷上传端点：`POST /api/v1/vault/import`

议题跟踪里已经有人在探索的方向：

- 把 CDS Hooks 接到 Epic 或 Cerner（我们出 FHIR R4 适配器，EHR 侧的胶水你自己写）
- 把默认 LLM 换成微调的领域模型
- 把审计链导入外部 SIEM（Splunk / Sentinel）
- 把 SQLite 换成 Postgres 以适应医院规模的多租户
- 给安全规则引擎加 SNOMED CT 概念查询

带测试和安全说明的 PR 一周内审完。

---

## 商业使用

代码 Apache-2.0。医院内部用、做成托管服务卖、裹进产品卖、嵌入更大系统——按你的商业模式来。维护者提供付费支持合同（响应 SLA、安全审查、版本钉死），有需要的机构联系我们（GitCode 议题或 pyproject.toml 里的邮箱）。

我们不接受的：

- 卖单个 PHI 记录或从它们训练出来的模型数据。
- 不回馈 bug fix 的闭源 fork。
- 在衍生作品中**移除审计链或确定性安全规则**。

最后一条是我们唯一会强制执行的。其余都是社区约定。

---

## 合规现状（当前）

| 标准 | 状态 | 备注 |
|---|---|---|
| HIPAA Safe Harbor（去标识化） | **部分实现** | 19 类标识符的正则/启发式脱敏（含身份证号）。**注意**：姓名识别默认基于称谓与姓氏表，漏检率未达 Safe Harbor 的法律标准；可配置 `DEID_SPACY_MODEL` 启用 spaCy NER 层提升召回，生产环境仍建议人工抽检。 |
| CDS Hooks 2.0 | 已实现 | `/cds-services` 端点按规范暴露。 |
| FHIR R4 + SMART-on-FHIR | 已实现 | 资源处理器在 `doctoragent/api/fhir/`。 |
| SNOMED CT / LOINC / ICD-10-CM | 已实现 | 术语绑定 + 查询辅助。 |
| 审计链防篡改 | 部分实现 | HMAC-SHA256 链式签名；密钥默认由主密钥 HKDF 派生（v0.3.8 起）。边界：持有主密钥口令的 root 仍可重签，需远程 append-only 副本方可对抗。 |
| RBAC 角色控制 | 部分（v0.3.7 起） | ADMIN 角色已接线租户创建/密钥轮换等管理端点；OIDC 用户按角色判定，静态 token 为服务账号语义。其余端点仍为单一 token 全权。 |
| 等保三级 | Roadmap | 2026 Q2 目标。可按需提供自评清单。 |
| HIPAA 第三方认证 | 未启动 | 需要有 PHI 的生产客户赞助。 |
| FDA 510(k) / NMPA / CE | Roadmap | 需要有书面工作流的临床试点。 |
| IRB 预批（美国） | 不适用 | 我们不出可直接用于人体研究的临床工作流。 |

"Roadmap"和"部分实现"都明明白白写出来，因为装作已经做好了只会浪费你的规划周期。

---

## 测试姿态

- **2314+ 个单元测试**，在 Python 3.10/3.11/3.12/3.13 全过。
- 控制台里 **66 条 API 路由**有完整往返测试。
- **18 个 CRUD 工作流**（临床工作台、文档库、智能体、钩子等）端到端跑过。
- **空壳扫描** 是 CI 一步——你 PR 加了一个不干活的端点或函数，测试会失败。
- **依赖审计** 也是 CI 一环。关键安全依赖钉版本，传递依赖升级 48 小时内评审。

---

## 常见问题

**免费吗？能商用吗？**
能。Apache-2.0 许可，商用、托管服务、嵌入产品都行。衍生作品的底线见[商业使用](#商业使用)三条。

**不用 LLM 能用吗？**
确定性安全引擎（药物相互作用/危急值/过敏交叉/重复用药）和文档库**完全离线可用**，不需要 API key、不需要网络。智能对话等智能体功能需要接 LLM 后端：任何 OpenAI 兼容端点都行，包括本地 Ollama（可完全断网运行）。

**支持哪些大模型？**
任何 OpenAI 兼容 API：Ollama（本地）、OpenAI、或你自己的网关。控制台"连接"里可配置多个 provider 并切换。

**能接医院系统吗？**
自带 CDS Hooks 2.0 端点（`/cds-services`）、FHIR R4 资源处理器和 SMART-on-FHIR 认证。把 EHR 的 CDS Hooks 客户端指向本服务属于集成实施工作，协议侧已就绪。

**患者数据怎么保护？**
PHI 脱敏（基于 HIPAA Safe Harbor 类别的正则启发式实现，19 类标识符含身份证号，4 种策略：redact/mask/pseudonymize/hash；法律合规性见上方合规现状表）、AES-256-GCM 加密存储、HMAC-SHA256 签名审计链、RBAC + API Token + 租户隔离。设计目标就是敏感数据可以不出你的基础设施。

**有认证吗？**
还没有。FDA 510(k)、HIPAA 第三方认证、等保三级都在 roadmap 里，见[合规现状](#合规现状当前)。支持试点部署，不宣称已获监管批准。

---

## 相关项目

- [badhope/AI](https://gitcode.com/badhope/AI) — DoctorAgent 用到的方法论、规则集、提示词库。
- [badhope](https://gitcode.com/badhope) — 其他 16 个开源项目。

---

## 许可证

Apache-2.0，见 `LICENSE`。内置的 SNOMED CT、LOINC、ICD-10-CM 参考数据按各自许可证再分发（见 `docs/COMPLIANCE_ROADMAP.md`）。

---

> **临床使用声明**：本系统是临床决策支持（CDS）工具，**不替代医生判断**。所有 AI 生成的建议仅供参考，最终决策由执业医师负责。