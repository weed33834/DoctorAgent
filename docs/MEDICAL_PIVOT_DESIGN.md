# DoctorAgent → 临床智能体平台转向设计文档

> ⚠️ **历史文档（Design Archive）**：本文为临床转向的原始设计草案，保留用于追溯决策依据。
> 当前能力的权威说明以 [CLINICAL_CAPABILITIES.md](CLINICAL_CAPABILITIES.md) 为准 ——
> 文中「阶段 1（本次实施重点）」的全部交付物均已落地并经测试，依赖清单与模块结构以
> `pyproject.toml` 与 `doctoragent/clinical/` 实际代码为准。

> 设计依据：AI-rule agent-builder INIT-PROMPT（20 项交付物）+ research.md（多源交叉验证）+ workflow-design.md（工作流编排模式）
> 调研来源：HL7 官方 FHIR 实现列表、openFDA/RxNorm 官方文档、Banalabs/Vstorm/eZintegrations 2026 医疗 AI 落地架构、MediSync/MedRAG/CDSS-agentic-rag/SafeRx-Agent 开源对标、arXiv 2605.29146（SafeRx-Agent）

---

## 一、战略定位与护城河（为什么是我们，凭什么赢）

### 1.1 一句话定位

**DoctorAgent = 合规优先、本地部署、可审计的临床智能体平台**（Compliance-first, on-premise, auditable clinical AI agent platform）

不与通用医疗 chatbot（OpenEvidence/Glass Health）比对话流畅度，而是**用别人临时拼不出的合规内建栈，切入医院/诊所真正敢部署的场景**。

### 1.2 与对标产品的差异化（核心护城河）

| 能力维度 | MediSync | MedRAG | CDSS-agentic-rag | SafeRx-Agent | **DoctorAgent（我们）** |
|---|---|---|---|---|---|
| HIPAA 加密（传输+存储+流式AEAD） | ✗ 文档说说 | ✗ | 依赖 Azure | ✗ 论文 | ✅ **已实现并测试** |
| 不可篡改审计日志（HMAC+轮转） | ✗ | ✗ | ✗ | ✗ | ✅ **已实现** |
| RBAC + OIDC SSO | ✗ | ✗ | Azure AD 锁定 | ✗ | ✅ **已实现** |
| 本地优先（Ollama，PHI 不出院） | 部分 | ✗ 云 | ✗ Azure | ✗ | ✅ **架构原生** |
| 多端 CRDT 同步（多科室/多终端） | ✗ | ✗ | ✗ | ✗ | ✅ **已实现** |
| KMS 集成（AWS/Azure/GCP） | ✗ | ✗ | ✗ | ✗ | ✅ **已实现** |
| DLP（PHI 脱敏） | ✗ | ✗ | ✗ | ✗ | ✅ **已实现** |
| Agent 框架（ReAct+Plan+Reflection+MCP+checkpoint） | 单 LLM | 单 Agent | Azure Foundry 锁定 | 多 Agent 研究 | ✅ **成熟手搓引擎** |
| RAG（CRAG+Agentic+评测） | ✗ | 基础 RAG | Agentic RAG | ✗ | ✅ **CRAG+Agentic+RAGAS** |

**结论**：所有对标产品都缺"合规 + 本地 + 审计"三件套——这正是 doctoragent 已实现的，也是医院敢不敢部署的硬门槛。**我们的赢法不是比谁的诊断更准，而是比谁让医院敢上线**。

### 1.3 要强化的特色（投入重点）

1. **合规叙事可验证**：把 audit_log/DLP/RBAC 包装成"HIPAA audit trail"，并加 `compliance_report` 工具一键生成合规自检报告（别人说"我们重视安全"，我们能导出证据）
2. **PHI 最小必要原则**：实现 de-identification pipeline——发往外部 LLM 前自动脱敏（[PATIENT_8f3a] 占位符），这是 2026 HHS 提案方向，多数竞品没做
3. **确定性安全规则 + LLM 双层**：药品相互作用/过敏/禁忌用确定性规则（openFDA/RxNorm），LLM 只做推理——这是 MediSync/SafeRx-Agent 的核心安全模式，我们直接采用
4. **可审计的引证链**：每个临床建议附 FHIR 资源 ID + 文献 PMID + 指南条目，医生可追溯——比纯 LLM 答案可信

---

## 二、架构总览（doctoragent 资产复用 + 新增模块）

### 2.1 复用映射（不改内核，加适配层）

```
临床请求 ─► [新增 clinical/ 包]
              │
              ├─ FHIR 适配层 ──► 读 Patient/Observation/MedicationRequest...
              │                 写 DocumentReference/ClinicalImpression
              │
              ├─ 临床工具集 ──► check_drug_interactions (openFDA/RxNorm)
              │                 search_clinical_guidelines
              │                 generate_soap_note
              │                 code_icd10
              │
              ├─ 安全规则引擎 ─► 确定性临床规则（vitals/labs/DDI/allergy）
              │
              └─ 多临床 Agent ─► PatientHistoryAgent / DrugSafetyAgent
                                LiteratureAgent / DocumentationAgent
                                 │ 复用现有 OrchestratorAgent/WorkerAgent
                                 ▼
              ┌─────────── doctoragent 内核（全复用，不改）───────────┐
              │ 加密/审计/RBAC/RAG(ReAct)/CRDT同步/KMS/MCP/checkpoint │
              └─────────────────────────────────────────────────────┘
```

### 2.2 新增包结构

```
doctoragent/clinical/
├── __init__.py
├── fhir/                      # FHIR R4 适配层
│   ├── client.py              # FHIRClient（读/写资源，SMART on FHIR auth）
│   ├── resources.py            # 资源序列化/反序列化（用 fhir.resources 库）
│   └── parser.py               # EHR→文本 序列化（喂给 LLM）
├── knowledge/                 # 医学知识源（全用外部 API，不造库）
│   ├── openfda.py              # openFDA 药品标签/不良事件
│   ├── rxnorm.py               # RxNorm 药名标准化
│   ├── drug_interactions.py    # DDI 检测（openFDA + DDInter）
│   └── pubmed.py               # PubMed/PMC 文献检索
├── safety/                    # 确定性临床安全规则
│   ├── rules.py                # vitals/labs/DDI/allergy 规则引擎
│   ├── reference_ranges.py     # 检验/生命体征参考范围
│   └── guardrails.py           # LLM 输出临床护栏（禁忌检测、幻觉检测）
├── agents/                    # 临床多 Agent（复用 model/agent.py）
│   ├── orchestrator.py         # 临床编排 Agent（复用 OrchestratorAgent）
│   ├── history_agent.py        # 病史解读
│   ├── drug_safety_agent.py    # 用药安全
│   ├── literature_agent.py     # 文献检索（复用 RAG）
│   └── documentation_agent.py  # 病历文书（SOAP/ICD-10）
├── tools/                     # 临床工具（注册到 ToolRegistry）
│   └── registry.py             # 临床工具集注册
├── workflow/                  # 临床工作流（复用 orchestration/）
│   └── clinical_workflow.py    # CDSS 工作流图
├── deidentification.py        # PHI 脱敏 pipeline
└── compliance_report.py       # HIPAA 合规自检报告
```

---

## 三、外部依赖选型（不造轮子）

| 能力 | 选用库/数据源 | 来源验证 | 不造的轮子 |
|---|---|---|---|
| FHIR R4 资源模型 | `fhir.resources`（pydantic-based，HL7 官方推荐） | HL7 官方开源列表 | 不手搓 FHIR schema |
| 药品标签/不良事件 | openFDA REST API（免费，240 req/min） | FDA 官方 | 不建药品库 |
| 药名标准化 | RxNorm REST API（免费，无需 key，20 req/s） | NLM 官方 | 不做药名映射 |
| 药品相互作用 | openFDA + DDInter 2.0 | rxlabelguard 验证 | 不自研 DDI 算法 |
| 文献检索 | PubMed E-utilities（官方）+ `medkit`（OpenFDA/PubMed/ClinicalTrials SDK） | HL7/GitHub | 不自建索引 |
| 嵌入 | sentence-transformers（已有） | 已复用 | — |
| LLM | Ollama（本地）/ OpenAI 兼容（已有 provider） | 已复用 | — |
| 向量库 | SQLite（默认）/ Chroma（已有可选） | 已复用 | — |
| 临床文本 NER（可选） | Azure Text Analytics / spaCy-med7 | 按需 | 不自训 NER |

**pyproject.toml 新增 extra**：`clinical`（实际落地版本含 `fhir.resources` / `instructor` / `openai` / `langgraph` / `authlib`，openFDA/RxNorm/PubMed 走 httpx，已在 core；设计稿中提到的 `medkit` 最终未采用，改用 httpx 直连官方 API）。详见 `pyproject.toml` 的 `[project.optional-dependencies] clinical`。

---

## 四、关键模块设计（按 agent-builder 规则）

### 4.1 临床工具集（§5 工具定义，≤15 个，五级副作用标注）

| 工具 | 副作用等级 | 用途 |
|---|---|---|
| `read_patient_record` | 只读 | 读 FHIR Patient/Condition/Encounter |
| `read_medications` | 只读 | 读 MedicationRequest/Dispense |
| `read_allergies` | 只读 | 读 AllergyIntolerance |
| `read_lab_results` | 只读 | 读 Observation(lab) |
| `check_drug_interactions` | 只读（网络） | openFDA+RxNorm 查 DDI |
| `search_clinical_guidelines` | 只读 | PubMed/指南库检索 |
| `search_literature` | 只读 | PubMed 文献检索（复用 RAG） |
| `check_vitals` | 只读 | 生命体征规则评估 |
| `check_lab_ranges` | 只读 | 检验值异常判定 |
| `generate_differential_diagnosis` | 安全写 | LLM 生成鉴别诊断（附置信度+证据） |
| `generate_soap_note` | 安全写 | 生成 SOAP 病历 |
| `code_icd10` | 安全写 | 自动 ICD-10 编码 |
| `write_clinical_note` | **破坏性写（需人工确认）** | 写 FHIR DocumentReference |
| `flag_safety_alert` | 安全写 | 标记临床安全警报 |
| `compliance_self_check` | 只读 | HIPAA 合规自检报告 |

### 4.2 安全护栏（§8，重点强化）

1. **确定性规则优先**：DDI/过敏/禁忌/生命体征危急值用规则引擎判定，LLM 不得覆盖
2. **所有写操作 human-in-loop**：`write_clinical_note`/任何 FHIR 写入必须人工确认后才执行（复用 human_in_loop + checkpoint）
3. **引证强制**：临床建议必须附 FHIR 资源 ID 或文献 PMID，无来源降级为"需医生确认"
4. **PHI 脱敏**：发往外部 LLM 前 deidentification.py 替换 10 类核心临床 PHI 为占位符（Safe Harbor 子集）
5. **提示注入防御**：FHIR 数据/文献打 `[UNTRUSTED]` 标记，检测覆盖指令

### 4.3 多临床 Agent 编排（§4 推理 + workflow-design 工作流模式）

复用现有 `OrchestratorAgent`/`WorkerAgent` + `dag_engine`，定义临床工作流图：

```
[临床请求] ─►[OrchestratorAgent 分流]
                 ├─►[PatientHistoryAgent]──┐ (并行 Fan-out)
                 ├─►[DrugSafetyAgent]─────┤
                 ├─►[LiteratureAgent]─────┤
                 ├─►[GuidelineAgent]──────┘
                                              ▼
                                    [Synthesis 综合节点] (Fan-in)
                                              │
                                     [Safety 审查节点] (条件分支)
                                       ├─ 通过 ─►[输出+引证]
                                       └─ 风险 ─►[人工确认节点](暂停 checkpoint)
```

- 顺序/并行/条件分支/人工审批四模式齐全（符合 workflow-design 检查清单）
- checkpoint 已实现，支持长流程断点续跑

### 4.4 幻觉检测（§1+§12，三层）

1. **SelfCheckGPT 多次采样一致性**：对数字类输出（剂量/编码/检验值）多次采样比对
2. **输出-来源支撑度**：Vectara HEM 式，检查引证是否真支撑结论
3. **RAGAS 四维**：复用现有 [evaluation.py](file:///workspace/doctoragent/doctoragent/model/rag.py) 的 faithfulness/context_precision/recall
- 检测失败降级："我需要确认这个信息" + 不确定性标注

### 4.5 评估套件（§12，≥20 case + 对抗测试）

- `tests/clinical/test_evaluation.py`：20+ golden cases（含对抗：提示注入/PII 提取/越权）
- 三道判定：正则黑名单 → 语义必中 → LLM-as-judge（judge 与被测不同模型族）
- 多维雷达：正确性/引证完整性/安全性/合规率

### 4.6 可观测性（§10，已实现，新增临床 span）

复用 [observability/](file:///workspace/doctoragent/doctoragent/observability/) 的 OTel + prometheus，加 `clinical_agent_span` 类型，trace 每次 FHIR 读写/DDI 查询/安全规则触发。

---

## 五、分阶段实施计划

### 阶段 1：比赛/演示可用（本次实施重点）

| 模块 | 内容 | 复用 |
|---|---|---|
| FHIR 适配层 | client + resources + parser（读 7 资源，写 DocumentReference） | fhir.resources 库 |
| 知识源 | openfda + rxnorm + drug_interactions + pubmed | httpx + 外部 API |
| 安全规则引擎 | rules + reference_ranges + guardrails | — |
| 临床工具集 | 15 工具注册到 ToolRegistry | 现有 tools.py |
| 临床多 Agent | 5 Agent + 编排工作流 | OrchestratorAgent/WorkerAgent |
| PHI 脱敏 | deidentification pipeline | DLP 扩展 |
| 合规报告 | compliance_self_check | audit_log |
| 演示数据 | 合成 FHIR 患者数据 + 测试用例 | — |
| 文档 | README + 临床能力说明 | — |

### 阶段 2：试点对接（后续，非本次）
- 接开源 FHIR 服务器（HAPI FHIR / IBM FHIR Server）
- MIMIC-IV 公开病历做 RAG 验证
- 接 1 家诊所试点

### 阶段 3：企业级认证（后续）
- HIPAA / 等保三级认证
- 商业 EMR（Epic/Cerner）FHIR 对接

---

## 六、约束与真实性声明

- 所有外部 API（openFDA/RxNorm/PubMed）均经官方文档验证存在
- `fhir.resources` 在 HL7 官方开源实现列表中（已验证）
- 临床建议仅供参考，不替代医生诊断——所有输出附免责声明 + 人工复核
- 阶段 1 不做真实诊断，聚焦"文档管理 + 用药安全 + 文献检索 + 合规审计"
- 遵循 AI-rule：不硬编码密钥、不外发 PHI、不绕过护栏

---

## 七、实施顺序（本次执行）

1. pyproject 加 `clinical` extra + 装依赖
2. `clinical/fhir/` 适配层（client/resources/parser）
3. `clinical/knowledge/` 知识源（openfda/rxnorm/drug_interactions/pubmed）
4. `clinical/safety/` 安全规则引擎
5. `clinical/tools/` 工具注册
6. `clinical/agents/` 多临床 Agent + 工作流
7. `clinical/deidentification.py` + `compliance_report.py`
8. 演示数据 + 测试用例（≥20）
9. 文档更新 + 提交
