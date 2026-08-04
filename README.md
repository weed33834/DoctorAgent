# ⚕️ DoctorAgent — 临床 AI 智能体平台

[English](README.md) | [中文](README.zh.md)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![FHIR R4](https://img.shields.io/badge/FHIR-R4-1d4ed8)](https://hl7.org/fhir/R4/)
[![CDS Hooks 2.0](https://img.shields.io/badge/CDS_Hooks-2.0-2563eb)](https://cds-hooks.hl7.org/)
[![MCP](https://img.shields.io/badge/MCP-7_tools-8b5cf6)](https://modelcontextprotocol.io/)

**企业级临床决策支持 AI 平台——加密存储、可审计、本地部署、Web 控制台。**

> 开箱即用的 27 模块 Web 控制台 / AES-256-GCM 加密 / HMAC 审计链 / PHI 脱敏 / 药物相互作用 / LLM 多智能体协作 / MCP 工具 / CDS Hooks 2.0 / FHIR R4

---

## 为什么选择 DoctorAgent

| 痛点 | DoctorAgent 方案 |
|---|---|
| LLM 调用数据泄露 | 所有数据本地加密，API 密钥 DPAPI 密封，云端需要显式授权 |
| 临床 LLM 不可信 | 确定性规则引擎优先于 LLM，5 层安全护栏 |
| 部署复杂 | `bash start.sh` 一键启动，无 Docker 依赖 |
| 功能分散 | 27 个模块统一 Web 控制台，侧边栏导航 + 搜索 |
| 合规审计困难 | HMAC 防篡改审计日志、6 篇合规教程、应用内合规看板 |

---

## 快速开始

```bash
git clone https://gitcode.com/badhope/AI.git DoctorAgent
cd DoctorAgent

# 安装依赖
pip install doctoragent[server]

# 配置 LLM（可选，本地即可运行确定性规则）
# 在 Web 控制台 → 连接 → 添加 OpenAI 兼容 API

# 启动
bash start.sh
```

打开 [http://127.0.0.1:8000/console/](http://127.0.0.1:8000/console/) 进入控制台。

---

## Web 控制台 — 27 个模块一览

```
┌──────────────┐  ┌──────────────────────────────────────────┐
│ 🔍 搜索功能.. │  │                                          │
│              │  │   ⚕ DoctorAgent 控制台    🏥 医生 ⚙️ 管理  │
│ 临床工具      │  │  ─────────────────────────────────────── │
│ 💬 智能对话   │  │                                          │
│ 🏥 临床工作台 │  │         主内容区                          │
│ 🔒 PHI 脱敏  │  │                                          │
│ 🛡 安全规则   │  │   27 个功能模块按需切换                   │
│ 🔀 智能体编排 │  │                                          │
│              │  │                                          │
│ 知识 & 检索   │  │                                          │
│ 📁 文档 Vault │  │                                          │
│ 🧠 高级 RAG  │  │                                          │
│ 🕸 知识图谱   │  │                                          │
│ 🧩 记忆管理   │  │                                          │
│ 📝 Prompt模板 │  │                                          │
│              │  │                                          │
│ 工作流 & 评估 │  │                                          │
│ ⚡ 工作流引擎 │  │                                          │
│ 📊 评估中心   │  │                                          │
│ 🔄 自进化    │  │                                          │
│ 🎯 强化学习   │  │                                          │
│ 👥 多智能体   │  │                                          │
│              │  │                                          │
│ 运维 & 管理   │  │                                          │
│ ⚙️ 配置管理   │  │                                          │
│ 🔗 连接      │  │                                          │
│ 🏢 租户      │  │                                          │
│ 📡 系统状态   │  │                                          │
│ 📋 审计日志   │  │                                          │
│ ✅ 合规管理   │  │                                          │
│ 🔧 集成运维   │  │                                          │
│              │  │                                          │
│ 扩展         │  │                                          │
│ 🎛 设置中心   │  │                                          │
│ 🪝 生命周期钩子│  │                                          │
│ 📈 可观测性   │  │                                          │
│ 🧩 插件管理   │  │                                          │
│ 🧪 A/B 实验  │  │                                          │
│              │  │                                          │
│ ── Token ──  │  │                                          │
└──────────────┘  └──────────────────────────────────────────┘
```

### 临床工具区

- **智能对话** — 多智能体 ReAct 循环，SSE 流式响应，文件上传、联网搜索
- **临床工作台** — FHIR 患者数据 + LLM 诊断建议 + 可视化图表
- **PHI 脱敏** — 4 种策略（Redact/Mask/Pseudonymize/Hash），HIPAA Safe Harbor
- **安全规则** — 5 项确定性检测（危急值/检验异常/DDI/过敏交叉/重复用药）
- **智能体编排** — LangGraph 拓扑可视化，9 节点 × 10 边

### 知识 & 检索区

- **文档 Vault** — 入库→分类→AES-256-GCM 加密→FTS5 索引→RAG 问答，全链 HMAC 审计
- **高级 RAG** — HyDE 查询重写 + RRF 融合 + 交叉编码重排 + Self-Corrective RAG
- **知识图谱** — 文档实体关系自动提取 + 图遍历检索
- **记忆管理** — 事实/情景/会话三层记忆，语义召回
- **Prompt 模板** — 创建→编辑→渲染→版本历史，变量支持

### 工作流 & 评估区

- **工作流引擎** — 优先级 DAG 调度器，超期任务自动提权
- **评估中心** — 多指标评估（准确率/召回率/相关性），阈值自定义
- **自进化** — 轨迹分析→经验提取→Prompt 优化→Experience 存储
- **强化学习** — 用户反馈驱动策略迭代
- **多智能体协作** — 角色委派，LLM 真实回复

### 运维 & 管理区

- **配置管理** — 完整 JSON 编辑器，模型/安全/集成全配置
- **连接** — 多 LLM 后端管理（OpenAI/Ollama/OpenAI Compatible），密钥 DPAPI 密封
- **租户** — 多租户数据隔离，Key Provider 可切换
- **系统状态** — 健康检查 + Pipeline Pool + 审计统计仪表板
- **审计日志** — 按事件类型筛选 + HMAC 完整性校验 + NDJSON 导出
- **合规管理** — 6 项合规检查（NMPA/算法备案/等保/IRB/数据安全/HIPAA），含完整教程
- **集成运维** — P2P 同步 + Webhook + 远程备份

### 扩展区

- **设置中心** — 系统提示词/Skill 编辑器/MCP 工具/高级配置
- **生命周期钩子** — 15 种钩子类型，Python 脚本注入
- **可观测性** — Traces + Logs + Prometheus 指标
- **插件管理** — 7 个内置插件，版本管理
- **A/B 实验** — 多变体分配，流量控制

---

## 架构亮点

```
入站文档 → 分类(LLM) → AES-256-GCM 加密 → FTS5/向量索引 → Vault
                ↓                                        ↓
         HMAC 审计链 ← ← ← ← ← ← ← ← ← ← ← ← ← RAG 问答
                ↓                                        ↓
         确定性规则引擎 ← ← 安全护栏 ← LLM 回复 ← CDS Hooks
```

- **确定性优先**：危急值/DDI/重复用药等规则引擎结果优先于 LLM 推断
- **加密落地**：AES-256-GCM，密钥经 PBKDF2-SHA256 派生，DPAPI 密封
- **审计全链**：`file_ingested → classified → encrypted → searched` 全程 HMAC 签名
- **CDS Hooks 2.0**：3 个钩子服务（patient-view / order-select / order-sign）
- **MCP 协议**：7 个工具（search/list/analyze/compare/memory/extract），JSON-RPC 调用

---

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | 原生 HTML/CSS/JS（零框架依赖），Chart.js |
| 后端 | Python 3.10+, FastAPI, Pydantic v2 |
| 数据库 | SQLite + FTS5 + 自定义向量存储 |
| 加密 | AES-256-GCM, PBKDF2-SHA256, HMAC-SHA256, DPAPI |
| LLM | OpenAI Compatible / Ollama / Anthropic |
| 协议 | FHIR R4, CDS Hooks 2.0, MCP |

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev,server]"

# 运行测试
pytest tests/ -q

# 启动开发服务器
bash start.sh
```

详见 [CONTRIBUTING.md](CONTRIBUTING.md)

---

## 许可证

MIT License - 详见 [LICENSE](LICENSE)

> ⚠️ **临床使用声明**：本系统为临床决策支持工具（CDS），不替代医生诊断。所有 AI 建议仅供参考，最终决策由执业医师负责。
