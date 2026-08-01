# DoctorAgent

[English](README.md) | [中文](README.zh.md) | [日本語](README.ja.md)

[![Tests](https://img.shields.io/badge/tests-2314%20passed-brightgreen.svg)](https://github.com/weed33834/DoctorAgent)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)
[![FHIR](https://img.shields.io/badge/FHIR-R4-1d4ed8.svg)](https://hl7.org/fhir/R4/)

**合规优先、本地部署、可审计的临床智能体平台。** 在企业信任边界内部署临床决策支持与文档智能体的本地优先框架——加密存储、防篡改审计日志、RBAC + OIDC SSO、KMS、PHI 脱敏、确定性安全规则与 LLM 输出护栏。未经显式授权 PHI 不会离开主机。完整临床能力说明见 [docs/CLINICAL_CAPABILITIES.md](docs/CLINICAL_CAPABILITIES.md)。

---

## 它能做什么

DoctorAgent 在一个加固核心之上提供两个协作面：

1. **临床 AI 智能体平台**（`doctoragent.clinical`）—— FHIR R4 适配器、确定性临床规则引擎（生命体征 / 检验 / 药物相互作用 / 过敏交叉反应 / 重复用药）、LLM 输出护栏（引用核验 / 禁止内容 / PHI 泄漏 / 提示注入）、15 个临床工具，以及多智能体工作流（扇出到专科智能体：病史、用药安全、文献、文书，再综合为带护栏、引用与免责声明的结果）。
2. **加密文档库**（DoctorAgent 核心）—— 投入监视目录的文件会被分类、用 AES-256-GCM 加密、索引（SQLite + FTS5）并归档到本地。检索为自然语言搜索与工具调用智能体。

临床工作流入口：

```python
from doctoragent.clinical.agents import run_clinical_workflow
result = await run_clinical_workflow(
    patient_context={"patient_id": "synthetic-001", "medications": [...], ...},
    query="该患者用药是否安全？",
    llm_provider=your_local_ollama_provider,  # None = 纯确定性安全规则，安全默认值
)
```

文档检索：

```bash
doctoragent ask "去年的房租合同在哪"
doctoragent agent "把所有财务相关的文件整理一下"
```

云端连接默认关闭。除非运维方显式授权，否则数据不会离开本机。

> ⚠️ 本系统为临床决策支持工具（CDS），不替代医生诊断。最终决策由医生负责。

---

## 快速开始

```bash
# 安装（需要 Python 3.10+）
pip install doctoragent[gui,server]

# 启动本地模型（Ollama 最为简单）
ollama pull qwen3:8b
ollama serve

# 启动 DoctorAgent，向 ~/DoctorAgent/Inbox 投入文件即可
doctoragent daemon
```

首次运行会自动创建目录结构。文件进入 Inbox 后会被自动分类、加密、归档。

---

## 主要功能

**临床 AI 智能体平台**（`doctoragent.clinical`）
- FHIR R4 适配器（HL7 官方 `fhir.resources`，SMART-on-FHIR bearer 认证）
- CDS Hooks 2.0 服务（patient-view / order-select / order-sign）
- 知识源：openFDA、RxNorm、PubMed（无专有数据库）
- 确定性安全规则：生命体征 / 检验 / 药物相互作用 / 过敏交叉反应 / 重复用药
- LLM 输出护栏：引用 / 禁止内容 / PHI 泄漏 / 提示注入（取最严格动作）
- 15 个临床工具，五级副作用标注（读 / 安全写 / 破坏性写需人工确认）
- 4 个专科智能体 + `ClinicalOrchestrator`（扇出 / 扇入 + 确定性安全 + 护栏复核）
- HIPAA Safe Harbor PHI 脱敏管线（10 类核心临床标识符）
- 合规自查报告（可导出审计证据）
- 22 个金标准评测用例，含对抗样本（提示注入、PII 提取、越权）
- 临床 QA 基准框架（MedQA / PubMedQA）：准确率 / macro-F1 / 校准（ECE+Brier）/ 安全 / 引用 / 延迟，跨模型族 LLM-as-judge
- 6 套合成 FHIR R4 测试数据，支持离线演示

**文件管理**
- 实时监控 Inbox 目录，自动处理新文件
- AES-256-GCM 加密存储，原子写入
- 三层密钥体系（主密钥 → 库密钥 → 文件密钥）
- SQLite + FTS5 全文搜索
- 防篡改审计日志

**智能检索**
- RAG 知识库：检索、过滤、重排、生成答案、引用来源
- 混合检索：关键词 + 语义向量，支持自定义权重
- 四层记忆系统：短期、工作、情景、长期记忆

**Agent 系统**
- ReAct 推理循环，多步骤任务执行
- JSON Schema 工具定义，兼容 OpenAI/Anthropic 函数调用
- Plan-and-Execute、深度反思、并行工具执行、错误恢复 + 熔断器
- 多智能体编排器/工作器模式，支持检查点持久化
- MCP 服务器（Model Context Protocol）实现工具互操作

**安全与合规**
- 本地优先，云端连接默认关闭
- Linux bubblewrap / Windows AppContainer 沙箱
- 主密钥轮换（定时 + 紧急）
- 多租户隔离与合规审计
- RBAC 权限矩阵 + OIDC SSO（Authlib + Casbin）
- 云端 KMS 抽象（AWS / Azure / GCP）
- PHI 脱敏（Safe Harbor 10 类核心临床标识符），用于临床工作流

**界面**
- PyQt6 桌面 GUI（系统托盘 + 文件浏览器）
- REST API（FastAPI）
- 命令行工具

---

## 安装

```bash
# 基础版
pip install doctoragent

# 临床 AI 智能体平台（FHIR R4 + openFDA/RxNorm/PubMed + 规则引擎 + 护栏）
pip install doctoragent[clinical]

# 桌面 GUI
pip install doctoragent[gui]

# 语义搜索
pip install doctoragent[semantic]

# REST API
pip install doctoragent[server]

# 全部功能
pip install doctoragent[gui,semantic,sync,server,multimodal,clinical]
```

Docker：

```bash
docker build -t doctoragent .
docker run --rm -it \
  --user $(id -u):$(id -g) \
  -v /path/to/inbox:/inbox \
  -v /path/to/vault:/vault \
  doctoragent daemon --no-tray
```

---

## 配置

环境变量优先级高于配置文件：

| 变量 | 说明 |
|------|------|
| `DOCTORAGENT_PATHS__INBOX` | Inbox 目录 |
| `DOCTORAGENT_PATHS__VAULT` | Vault 目录 |
| `DOCTORAGENT_SECURITY__MASTER_KEY_PROVIDER` | 主密钥提供者（`filepassword`、`dpapi`、`tpm`、`mac-keychain`） |
| `DOCTORAGENT_MODEL__BASE_URL` | 模型地址 |
| `DOCTORAGENT_MODEL__MODEL_NAME` | 模型名称 |

配置文件位于 `~/DoctorAgent/Config/settings.json`。敏感信息（主密钥口令、Webhook 共享密钥、S3/WebDAV 凭据）不会被写入磁盘，须通过环境变量提供。

---

## 命令行

| 命令 | 用途 |
|------|------|
| `doctoragent daemon` | 启动 Agent 监控 Inbox |
| `doctoragent ask` | RAG 问答 |
| `doctoragent agent` | 带工具调用的智能代理 |
| `doctoragent search` | 搜索文件 |
| `doctoragent status` | 查看状态 |
| `doctoragent list` | 列出文件 |
| `doctoragent export` | 导出文件 |
| `doctoragent import` | 批量导入 |
| `doctoragent pipe` | 从标准输入导入 |
| `doctoragent run` | 执行 JSON 编排脚本 |
| `doctoragent serve` | 启动 API 服务 |
| `doctoragent backup` | 远程备份 |
| `doctoragent webhook-test` | 触发测试 Webhook |

```bash
# 搜索
doctoragent search "invoice 2024"
doctoragent search "rent contract" --semantic --top-k 10

# 问答
doctoragent ask "summarise last quarter's invoices"

# Agent
doctoragent agent "Analyze all my contracts and identify key dates" --verbose
```

---

## API

`doctoragent serve` 启动 REST API（默认 `127.0.0.1:8000`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/metrics` | Prometheus 指标 |
| GET | `/vault/status` | 文件统计 |
| GET | `/vault/files` | 文件列表 |
| POST | `/vault/search` | 搜索（关键词 / 语义） |
| POST | `/vault/ask` | RAG 问答 |
| POST | `/vault/ask/stream` | 流式 RAG（SSE） |
| POST | `/vault/agent` | Agent 任务 |
| POST | `/vault/agent/stream` | 流式 Agent（SSE） |
| POST | `/clinical/analyze` | 临床工作流（规则 + 专科智能体 + 护栏） |
| GET | `/cds-services` | CDS Hooks 2.0 服务发现 |
| POST | `/cds-services/{id}` | CDS Hooks 调用（patient-view / order-select / order-sign） |
| GET | `/events` | 实时审计事件流（SSE） |
| WS | `/ws` | WebSocket（Agent + 事件推送） |
| POST | `/mcp` | MCP 工具服务器端点 |

设置 `DOCTORAGENT_API_TOKEN` 启用静态 bearer 认证，或设置 `DOCTORAGENT_OIDC_ISSUER` 启用 OIDC SSO。未配置 token 时，敏感端点要求可信本地连接（127.0.0.1）。

---

## 安全模型

- **本地优先**：数据默认不离开本机
- **三层密钥**：主密钥（Argon2id / DPAPI / TPM / Keychain）→ 库密钥（HKDF-SHA256）→ 文件密钥（HKDF-SHA256，按文件加盐）
- **加密存储**：AES-256-GCM，原子写入
- **审计日志**：追加式 NDJSON，每条记录附带 HMAC-SHA256，可离线检测篡改
- **网络隔离**：敏感操作要求可信本地连接（127.0.0.1）；云端回退需显式开启并按连接授权
- **沙箱**：Linux bubblewrap / Windows AppContainer
- **密钥轮换**：定时轮换（默认 90 天）与紧急轮换，采用全量重加密、失败即回滚

---

## 开发

```bash
git clone https://github.com/weed33834/DoctorAgent.git
cd DoctorAgent
pip install -e ".[gui,server,semantic,multimodal,dev]"

# 运行测试
python -m pytest tests/ -v

# 代码检查
ruff check doctoragent/
ruff format doctoragent/
```

详细指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 镜像 / Mirrors

本仓库主要托管在 **GitHub**，并镜像到 GitCode 与 Gitee 以提升可达性。

| 平台 | 地址 |
|----------|-----|
| **GitHub**（主仓库） | https://github.com/weed33834/DoctorAgent |
| GitCode（镜像） | https://gitcode.com/badhope/DoctorAgent |
| Gitee（镜像） | https://gitee.com/badhope/DoctorAgent |

> 各平台内容手动同步，GitHub 为权威来源。

---

## 许可证

[MIT License](LICENSE)
