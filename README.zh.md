# ⚕️ DoctorAgent — 临床 AI 智能体平台

[English](README.md) | [中文](README.zh.md)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![FHIR R4](https://img.shields.io/badge/FHIR-R4-1d4ed8)](https://hl7.org/fhir/R4/)
[![CDS Hooks 2.0](https://img.shields.io/badge/CDS_Hooks-2.0-2563eb)](https://cds-hooks.hl7.org/)
[![MCP](https://img.shields.io/badge/MCP-7_工具-8b5cf6)](https://modelcontextprotocol.io/)

**企业级临床决策支持 AI 平台——加密存储、可审计、本地部署、Web 控制台。**

> 开箱即用的 27 模块 Web 控制台 / AES-256-GCM 加密 / HMAC 审计链 / PHI 脱敏 / 药物相互作用 / LLM 多智能体协作 / MCP 工具 / CDS Hooks 2.0 / FHIR R4

---

## 快速开始

```bash
git clone https://gitcode.com/badhope/AI.git DoctorAgent
cd DoctorAgent
pip install doctoragent[server]
bash start.sh
```

浏览器打开 [http://127.0.0.1:8000/console/](http://127.0.0.1:8000/console/) 即可使用。

> 需要 LLM 对话功能？在控制台左侧「连接」→ 添加 OpenAI 兼容 API 密钥即可。
> 本地部署无需外部 API——确定性安全规则（危急值检测/DDI/过敏交叉/重复用药）离线可用。

---

## Web 控制台 — 27 个功能模块

**侧边栏导航 + 搜索过滤 + 一键折叠**，分组清晰：

### 临床工具
- **智能对话** — 多智能体 ReAct 循环，SSE 流式响应，支持文件上传
- **临床工作台** — FHIR 患者数据 + LLM 诊断建议 + 可视化图表
- **PHI 脱敏** — Redact/Mask/Pseudonymize/Hash 四种策略
- **安全规则** — 危急值检测/检验异常/药物联用/过敏交叉/重复用药
- **智能体编排** — LangGraph 拓扑可视化

### 知识 & 检索
- **文档 Vault** — 入库→分类→加密→索引→RAG 问答全链审计
- **高级 RAG** — HyDE + RRF 融合 + 交叉编码重排 + Self-Corrective
- **知识图谱** — 实体关系自动提取 + 图检索
- **记忆管理** — 三层记忆（事实/情景/会话）+ 语义召回
- **Prompt 模板** — 创建/编辑/渲染/版本历史

### 工作流 & 评估
- **工作流引擎** — DAG 优先级调度，超期自动提权
- **评估中心** — 多指标评估，阈值自定义
- **自进化** — 轨迹分析→经验提取→Prompt 优化
- **强化学习** — 反馈驱动策略迭代
- **多智能体协作** — 角色委派，LLM 真实回复

### 运维 & 管理
- **配置管理** — JSON 编辑器，全配置项可视化
- **连接** — 多 LLM 后端，密钥 DPAPI 密封
- **租户** — 多租户隔离，Key Provider 可切换
- **系统状态** — 健康/Pipeline/审计仪表板
- **审计日志** — 事件筛选 + HMAC 校验 + NDJSON 导出
- **合规管理** — NMPA/算法备案/等保/IRB/HIPAA，含完整教程
- **集成运维** — P2P 同步 + Webhook + 远程备份

### 扩展
- **设置中心** — 提示词/Skill/MCP/高级配置
- **生命周期钩子** — 15 种钩子，Python 脚本注入
- **可观测性** — Traces/Logs/Prometheus 指标
- **插件管理** — 7 个内置插件
- **A/B 实验** — 多变体分配

---

## 为什么选择 DoctorAgent

| 场景 | DoctorAgent 解决方式 |
|---|---|
| LLM 数据泄露风险 | 全链路本地加密，云端需显式授权 |
| 临床 AI 可信度不足 | 确定性规则优先于 LLM，5 层安全护栏 |
| 部署太复杂 | `bash start.sh` 一键启动 |
| 功能太分散 | 27 模块统一控制台，侧边栏搜索 |
| 合规审计困难 | HMAC 防篡改审计，6 篇合规教程 |

---

## 架构

```
入站文档 → 分类(LLM) → AES-256-GCM 加密 → FTS5/向量索引 → Vault
                ↓                                        ↓
         HMAC 审计链 ← ← ← ← ← ← ← ← ← ← ← ← RAG 问答
                ↓                                        ↓
         确定性规则引擎 ← 安全护栏 ← LLM 回复 ← CDS Hooks
```

- **确定性优先**：规则引擎结果覆盖 LLM 推断
- **加密落地**：AES-256-GCM，密钥 PBKDF2-SHA256 派生，DPAPI 密封
- **审计全链**：从入库到问答全程 HMAC 签名
- **CDS Hooks 2.0**：3 个钩子服务
- **MCP 协议**：7 个工具，JSON-RPC

---

## 技术栈

Python 3.10+ / FastAPI / Pydantic v2 / SQLite + FTS5 / AES-256-GCM / HMAC-SHA256 / Chart.js / FHIR R4 / CDS Hooks 2.0 / MCP

---

## 许可证

MIT License — 详见 [LICENSE](LICENSE)

> ⚠️ **临床使用声明**：本系统为临床决策支持工具（CDS），不替代医生诊断。所有 AI 建议仅供参考，最终决策由执业医师负责。
