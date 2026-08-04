# ⚕️ DoctorAgent — 企业级临床 AI 智能体平台

[English](README.md) | [中文](README.zh.md)

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python">
  <img src="https://img.shields.io/badge/License-MIT-green">
  <img src="https://img.shields.io/badge/FHIR-R4-1d4ed8">
  <img src="https://img.shields.io/badge/CDS_Hooks-2.0-2563eb">
  <img src="https://img.shields.io/badge/MCP-7_工具-8b5cf6">
  <img src="https://img.shields.io/badge/Tests-2314_passed-brightgreen">
</p>

<p align="center"><b>
  开箱即用 27 模块 Web 控制台 · AES-256-GCM 加密 · HMAC 审计链<br>
  PHI 脱敏 · 药物相互作用检测 · LLM 多智能体协作 · CDS Hooks 2.0
</b></p>

---

## 📦 仓库家族

本项目是 **badhope 开源 AI 生态** 的核心成员：

| 仓库 | 定位 | 链接 |
|---|---|---|
| 🏠 **AI** (Rule Hub) | **主仓库** — AI 工程方法论、规则集、提示词库、Skill 市场 | [gitcode.com/badhope/AI](https://gitcode.com/badhope/AI) |
| ⚕️ **DoctorAgent** | **核心产品** — 临床 AI 智能体平台（本仓库） | [gitcode.com/badhope/DoctorAgent](https://gitcode.com/badhope/DoctorAgent) |
| 🎨 *(待定)* | **可视化 & 展示** — 产品演示、截图、文档站点 | 建设中 |

> 💡 **使用指南**：AI 仓库 = 大脑（方法论 + 规则），DoctorAgent = 身体（落地产品），可视化仓库 = 门面（展示推广）。

---

## 🚀 快速开始

```bash
git clone https://gitcode.com/badhope/DoctorAgent.git
cd DoctorAgent
pip install doctoragent[server]
bash start.sh
```

浏览器打开 [http://127.0.0.1:8000/console/](http://127.0.0.1:8000/console/) 即可使用。

> 无需 LLM 也能运行——确定性安全规则（危急值/DDI/过敏/重复用药）**完全离线可用**。

---

## 🎯 功能全景

### 🏥 临床智能

| 功能 | 描述 | 技术亮点 |
|---|---|---|
| 智能对话 | 多智能体 ReAct 循环，SSE 流式响应 | 文件上传、联网搜索、会话管理 |
| 临床工作台 | FHIR 患者数据 + LLM 诊断建议 + 可视化 | 4 专科 Agent 并行推理 |
| PHI 脱敏 | 4 种策略（Redact/Mask/Pseudonymize/Hash） | 姓名/电话/身份证/地址全覆盖 |
| 安全规则 | 5 项确定性检测 | 危急值/检验/DDI/过敏交叉/重复用药 |
| 智能体编排 | LangGraph 拓扑可视化 | 9 节点 × 10 边 DAG |

### 📚 知识管理

| 功能 | 描述 | 技术亮点 |
|---|---|---|
| 文档 Vault | 入库→分类→加密→索引→RAG | AES-256-GCM + FTS5 + HMAC 全链审计 |
| 高级 RAG | HyDE + RRF 融合 + 交叉编码重排 | Self-Corrective 自纠错 |
| 知识图谱 | 实体关系自动提取 + 图遍历检索 | LLM + SQLite 持久化 |
| 记忆管理 | 事实/情景/会话三层记忆 | 语义召回 |
| Prompt 模板 | 创建→编辑→渲染→版本历史 | 变量动态渲染 |

### ⚡ 工作流

| 功能 | 描述 | 技术亮点 |
|---|---|---|
| 工作流引擎 | DAG 优先级调度 | 超期任务自动提权 |
| 评估中心 | 多指标评估 | 阈值自定义 |
| 自进化 | 轨迹分析→经验提取→优化 | 全自动闭环 |
| 强化学习 | 用户反馈驱动策略迭代 | RLHF |
| 多智能体协作 | 角色委派，LLM 真实回复 | 异步 delegate + provider |

### 🔧 运维

| 功能 | 描述 |
|---|---|
| 配置管理 | 完整 JSON 编辑器 |
| 连接管理 | 多 LLM 后端，密钥 DPAPI 密封 |
| 租户管理 | 多租户隔离 |
| 系统状态 | 健康/Pipeline/审计仪表板 |
| 审计日志 | 事件筛选 + HMAC 校验 + NDJSON 导出 |
| 合规管理 | 6 项合规检查 + 完整教程（8-14KB/篇） |
| 集成运维 | P2P 同步 + Webhook + 远程备份 |

### 🧩 扩展

| 功能 | 描述 |
|---|---|
| 设置中心 | 提示词/Skill/MCP/高级配置 |
| 生命周期钩子 | 15 种钩子类型，Python 脚本注入 |
| 可观测性 | Traces + Logs + Prometheus 指标 |
| 插件管理 | 7 个内置插件 |
| A/B 实验 | 多变体分配 |

---

## 🏗 架构

```
入站文档 → 分类器(LLM) → AES-256-GCM 加密 → FTS5/向量索引 → Vault
                ↓                                        ↓
         HMAC 审计链 ← ← ← ← ← ← ← ← ← ← ← RAG 问答
                ↓                                        ↓
         确定性规则引擎 ← 安全护栏(5层) ← LLM 回复 ← CDS Hooks
```

**核心原则**：确定性规则引擎结果优先于 LLM 推断，冲突时以规则为准。

---

## 🛡 安全与合规

| 能力 | 实现 |
|---|---|
| 传输加密 | HTTPS / TLS |
| 存储加密 | AES-256-GCM，密钥 PBKDF2-SHA256 派生 |
| 密钥管理 | DPAPI（Windows）/ Keychain（macOS）/ TPM / FilePassword |
| 审计 | HMAC-SHA256 签名链，防篡改，NDJSON 导出 |
| PHI 保护 | 4 种脱敏策略，HIPAA Safe Harbor |
| 访问控制 | RBAC + API Token + 租户隔离 |
| LLM 护栏 | 引用核验 / 禁止内容 / PHI 泄漏检测 / 提示注入防护 |
| 合规 | NMPA / 算法备案 / 等保三级 / IRB / 数据安全三法 / HIPAA |

---

## 📊 测试覆盖

| 维度 | 结果 |
|---|---|
| 路由连通 | 66 条前端 API 100% 可达 |
| 交互 CRUD | 18 个流程全部通过 |
| 跨模块集成 | 10 个端到端场景贯通 |
| 空壳扫描 | 38 个端点，0 个空壳 |
| 单元测试 | 2314 passed |

---

## 🔗 相关链接

- **主仓库**: [badhope/AI](https://gitcode.com/badhope/AI) — AI 工程方法论与规则集
- **文档**: [docs/](docs/)
- **贡献指南**: [CONTRIBUTING.md](CONTRIBUTING.md)
- **许可证**: [MIT](LICENSE)
- **问题反馈**: [GitCode Issues](https://gitcode.com/badhope/DoctorAgent/issues)

---

> ⚠️ **临床使用声明**：本系统为临床决策支持工具（CDS），不替代医生诊断。所有 AI 建议仅供参考，最终决策由执业医师负责。
