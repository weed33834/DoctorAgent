# DoctorAgent 临床医生使用手册

> 版本：对应临床规则知识库 v1.1.0 · 适用对象：临床医生、住院医、护士长
> 本手册面向医生在临床场景下使用 DoctorAgent 临床 AI 智能体。所有示例输入/输出均为脱敏的合成数据。

---

## 1. 概述

### 1.1 产品定位

DoctorAgent 是一个**合规优先、本地化部署**的临床 AI 智能体，部署在医疗机构自有基础设施内，PHI（受保护健康信息）原则上不离开可信边界。它为临床医生提供四类辅助能力：

| 能力域 | 说明 |
|---|---|
| 临床决策支持（Clinical Decision Support） | 用药安全、过敏交叉反应、危急值预警、重复治疗检测 |
| 病历生成（Documentation） | SOAP 病历草稿、ICD-10 编码建议（始终标注「待医生签发」） |
| 文献检索（Literature retrieval） | PubMed + 临床指南检索，每条建议附可追溯引证 |
| 多智能体协作（Multi-agent） | 4 个专科 Agent + ClinicalOrchestrator（fan-out/fan-in DAG） |

### 1.2 适用范围

- ✅ **适用于**：辅助医生整理病史、核查用药安全、检索文献证据、起草病历文书。
- ❌ **不适用于**：作为独立诊断工具、替代执业医师判断、自主下达医嘱或闭环处方。

> **核心定位声明**：DoctorAgent 是**辅助决策工具，非医疗器械诊断产品**。所有输出均为建议性质，最终临床决策由具备执业资格的医师承担。

### 1.3 谁应该读这本手册

- **临床医生 / 主治医师**：日常查房、用药安全核查、病历书写时使用。
- **住院医 / 规培医师**：学习结构化病史整理与证据检索。
- **护士长 / 临床药师**：理解安全预警含义，配合医生完成复核流程。

### 1.4 核心安全原则（4 条）

医生在使用本智能体前，必须理解并遵守以下四条原则：

1. **AI 不替代医生** —— 本智能体输出为辅助参考，不构成诊断或医嘱；所有临床行动须经执业医师签发。
2. **引证可追溯** —— 每条临床建议必须附可追溯引证（PMID / DOI / FHIR Resource/id / 指南机构）；无引证的输出会被自动降级为「需医生确认」。
3. **PHI 不外泄** —— 系统在调用任何外部服务前，自动对 18 类 HIPAA Safe Harbor 标识符进行脱敏；医生仍应遵循最小化原则，不在自由文本中输入不必要的 PHI。
4. **决策可审计** —— 每次查询、每条安全预警、每个 guardrail 动作均写入防篡改审计日志，符合 FDA SaMD / 21 CFR Part 11 / HIPAA 要求，决策链可完整重建。

---

## 2. 快速开始（5 分钟上手）

### 2.1 登录控制台

在浏览器打开 Web 控制台：

```
http://<server>:8000/console/
```

- 页面右上角输入管理员分配的 API Token，点击保存。
- 健康指示灯显示「已认证」即表示登录成功。
- 本地内网访问可免 Token；跨网段或带鉴权部署时必须配置 Token。

### 2.2 切换到「医生视图」

页面顶部有视图切换器：

- 🏥 **医生视图**（推荐临床使用，低认知负荷，5 个标签页）
- ⚙️ **管理视图**（治理用，21 个标签页，非临床日常使用）

医生视图包含 5 个聚焦标签页：

| 标签页 | 用途 |
|---|---|
| 临床工作台 | 录入患者信息、发起综合临床查询（主入口） |
| 病史检索 | 查看结构化病史摘要与问题清单 |
| 用药安全 | 专注用药相互作用 / 过敏 / 重复治疗核查 |
| 文献查询 | 按临床问题检索 PubMed 与指南 |
| 我的会话 | 历史会话回看与追溯 |

### 2.3 第一次提问：用药安全查询

进入「临床工作台」，使用**表单录入**（而非 JSON 文本框）填写患者信息：

- patient_id：`synthetic-001`
- medications（每行一条）：`Warfarin 5mg PO`、`Fluconazole 200mg PO`
- allergies：`Penicillin`
- query：`该患者用药是否安全？`

点击「运行临床工作流」，等待结果返回。

### 2.4 阅读输出

每次输出固定包含三类要素，医生必须逐一确认：

- **引证（citations）**：如 `PMID:12345678` 或 `指南: WHO`，可点击追溯。
- **免责声明（disclaimer）**：`本结果由 AI 生成，不替代医生临床判断，需经执业医师签发`。
- **requires_human_review 标志**：当值为 `True` 时，页面顶部显示**红色横幅**，强制人工复核后方可采信。

> ⚠️ **红色横幅出现时，禁止直接照搬 AI 输出下达医嘱**，必须完成人工复核流程。

---

## 3. 临床工作台详解

### 3.1 患者信息录入（表单字段逐项说明）

临床工作台采用**结构化表单**录入（降低出错率，便于审计），字段如下：

| 字段 | 类型 | 说明 | 示例值 |
|---|---|---|---|
| `patient_id` | string | 患者标识（建议使用脱敏 ID，勿填姓名） | `synthetic-001` |
| `age` | number | 年龄（岁） | `68` |
| `gender` | string | 性别：`male` / `female`（影响性别相关参考范围，如血红蛋白、肌酐） | `male` |
| `vitals` | object | 生命体征（见下表） | `{"heart_rate": 72}` |
| `labs` | array | 检验项列表，每项 `{test, value, unit}` | `[{"test":"sodium","value":160,"unit":"mmol/L"}]` |
| `medications` | array | 当前用药，支持品牌名与通用名（RxNorm 等价识别） | `["warfarin 5mg","ibuprofen 400mg"]` |
| `allergies` | array | 过敏史，支持 17 类交叉反应识别 | `["penicillin"]` |
| `query` | string | 自由文本临床问题 | `该患者用药是否安全？` |

**vitals 子字段**（生命体征）：

| 字段 | 单位 | 正常范围 | 危急阈值 |
|---|---|---|---|
| `heart_rate` | bpm | 60–100 | <40 或 >130 |
| `systolic_bp` | mmHg | 90–120 | <80 或 >180 |
| `diastolic_bp` | mmHg | 60–80 | <50 或 >120 |
| `temperature` | C | 36.1–37.2 | <35.0 或 >39.0 |
| `respiratory_rate` | rpm | 12–20 | <8 或 >25 |
| `spo2` | % | 95–100 | <90 |

**labs 示例**（一项一条，含 test / value / unit）：

```json
[
  {"test": "sodium", "value": 160, "unit": "mmol/L"},
  {"test": "potassium", "value": 6.8, "unit": "mmol/L"},
  {"test": "hemoglobin", "value": 65, "unit": "g/L"}
]
```

> 提示：表单提供「示例预设」下拉（safe / drug-interaction / allergy-alert / critical-vitals / critical-labs / duplicate-therapy），可一键填充合成数据用于学习。

### 3.2 输出区域解读

工作流返回一个结构化结果 `ClinicalWorkflowResult`，医生应按以下顺序阅读：

1. **`requires_human_review`**（最先看）—— `true` 时顶部红色横幅，强制复核。
2. **`guardrail_result.action`** —— 取值 `allow` / `flag` / `block`：
   - `allow`：输出可参考；
   - `flag`：输出降级，需医生确认（如缺引证）；
   - `block`：输出被阻断，仅显示警告（如检测到 PHI 泄露或明确诊断措辞）。
3. **`safety_findings`** —— 安全发现列表，按严重度排序：`contraindicated > critical > warning > info`。
4. **`history_summary`** —— 结构化病史摘要与问题清单。
5. **`literature`** —— 文献列表，按证据等级排序。
6. **`documentation`** —— SOAP 草稿 + ICD-10 候选编码，标注「待医生签发」。
7. **`citations`** —— 全部去重引证（PMID / DOI / FHIR Resource/id / 指南）。
8. **`disclaimer`** —— 免责声明（每次输出固定携带）。

完整输出示例（节选）：

```json
{
  "requires_human_review": true,
  "guardrail_result": {"passed": false, "action": "flag", "warnings": ["输出缺少引证，需医生确认"]},
  "safety_findings": [
    {
      "rule_type": "drug_interaction",
      "severity": "critical",
      "finding": "warfarin 与 ibuprofen 存在 major 级药物相互作用",
      "affected_resources": ["warfarin", "ibuprofen"],
      "recommendation": "评估替代方案或加强不良反应监测",
      "source": "rule_engine"
    }
  ],
  "history_summary": "患者既往房颤，长期华法林抗凝...",
  "literature": [{"title": "...", "source": "PMID:12345678", "evidence_level": "rct"}],
  "documentation": {"soap": {"subjective": "...", "objective": "...", "assessment": "...", "plan": "..."}, "icd10": ["I48.0"]},
  "citations": ["PMID:12345678"],
  "disclaimer": "本结果由 AI 生成，不替代医生临床判断，需经执业医师签发"
}
```

### 3.3 严重度图例

安全发现按严重度配色，医生应根据颜色采取对应行动：

| 严重度 | 颜色 | 含义 | 医生应做什么 |
|---|---|---|---|
| `contraindicated` | 🔴 红色 | 禁忌，阻断输出 | **禁用**该方案，必须改方案；不得继续 |
| `critical` | 🟠 橙红 | 危急，强制人工复核 | **立即复核**，可能需要紧急临床干预 |
| `warning` | 🟡 黄色 | 警示，提示风险 | **评估风险**，在病历中记录评估理由 |
| `info` | 🔵 蓝色 | 信息，知悉即可 | 知悉即可，无需额外行动 |

> 阻断级（`contraindicated` / `critical`）发现**始终**将 `requires_human_review` 置为 `true`，无论 LLM 给出什么建议，规则引擎结论优先。

---

## 4. 用药安全场景示例

### 4.1 药物相互作用查询（warfarin + ibuprofen）

**输入**：

```json
{
  "patient_id": "synthetic-ddi-001",
  "age": 72,
  "gender": "male",
  "medications": ["warfarin 5mg", "ibuprofen 400mg"],
  "allergies": [],
  "vitals": {"heart_rate": 78, "systolic_bp": 130, "diastolic_bp": 85},
  "labs": [{"test": "inr", "value": 2.5, "unit": ""}],
  "query": "该患者用药是否安全？检查药物相互作用。"
}
```

**预期输出（safety_findings 节选）**：

```json
{
  "rule_type": "drug_interaction",
  "severity": "critical",
  "finding": "warfarin 与 ibuprofen 存在 major 级药物相互作用",
  "recommendation": "评估替代方案或加强不良反应监测",
  "requires_human_review": true
}
```

**解读**：华法林与非甾体抗炎药联用显著增加出血风险。系统将其标为 `critical`（橙红），强制人工复核。医生应评估是否换用对乙酰氨基酚等替代方案，并加强 INR 与出血监测。

### 4.2 过敏交叉反应查询（青霉素过敏 + 阿莫西林）

**输入**：

```json
{
  "patient_id": "synthetic-allergy-001",
  "medications": ["Amoxicillin 500mg PO"],
  "allergies": ["Penicillin"],
  "query": "该患者用药是否安全？检查过敏交叉反应。"
}
```

**预期输出**：

```json
{
  "rule_type": "allergy",
  "severity": "contraindicated",
  "finding": "药物 Amoxicillin 500mg PO 与患者过敏原 Penicillin 存在交叉反应风险（amoxicillin）",
  "recommendation": "避免使用 Amoxicillin 500mg PO；如确需使用，须先经过敏专科评估"
}
```

**解读**：阿莫西林属青霉素类，同类直接过敏反应判为 `contraindicated`（红色阻断）。系统支持 **17 类过敏交叉反应**识别，含跨类反应（如青霉素↔头孢类判为 `warning`，交叉反应率 1–10%；造影剂、乳胶、神经肌肉阻滞剂、铂类再激发判为 `critical`，可致命）。跨类匹配可递归一层——例如患者对青霉素过敏而使用头孢曲松，也会命中。

### 4.3 重复治疗查询（Tylenol + Acetaminophen）

**输入**：

```json
{
  "patient_id": "synthetic-dup-001",
  "medications": ["Tylenol 500mg", "Acetaminophen 325mg"],
  "allergies": [],
  "query": "该患者是否存在重复用药？"
}
```

**预期输出**：

```json
{
  "rule_type": "duplicate_therapy",
  "severity": "warning",
  "finding": "重复治疗：Tylenol 500mg 与 Acetaminophen 325mg 含同一活性成分 (Acetaminophen)（RxNorm 等价识别）",
  "recommendation": "复核用药方案，避免重复治疗"
}
```

**解读**：Tylenol 是对乙酰氨基酚的品牌名。系统通过 **RxNorm** 将品牌名解析为活性成分（RxCUI）并做等价识别，捕获本地名匹配漏掉的「品牌↔通用名」重复。同类检测也可识别 Vicodin ↔ Norco 等多品牌重复。

### 4.4 危急值查询（heart_rate=35）

**输入**：

```json
{
  "patient_id": "synthetic-vitals-001",
  "medications": ["Warfarin 5mg PO"],
  "allergies": [],
  "vitals": {"heart_rate": 35, "systolic_bp": 88, "diastolic_bp": 55},
  "labs": [{"test": "potassium", "value": 6.8, "unit": "mmol/L"}],
  "query": "该患者生命体征是否危急？"
}
```

**预期输出**：

```json
[
  {
    "rule_type": "vitals",
    "severity": "critical",
    "finding": "heart_rate 35 bpm 低于危急值 40",
    "recommendation": "立即复核 heart_rate，评估紧急临床干预"
  },
  {
    "rule_type": "labs",
    "severity": "critical",
    "finding": "potassium 6.8 mmol/L 高于危急值 6.5",
    "recommendation": "立即复核 potassium，评估紧急临床干预"
  }
]
```

**解读**：心率 35 < 危急下限 40 → `critical`；血钾 6.8 > 危急上限 6.5 → `critical`。两项均为阻断级，强制人工复核。`requires_human_review` 必为 `true`。

---

## 5. 病历生成与签发流程

DoctorAgent 的病历生成遵循「**AI 起草 → 医生审核 → 修改 → 签发**」的人机闭环，**任何 FHIR 写回均需人工确认**，系统不会自主写入 EHR。

### 5.1 标准签发流程

- [ ] 1. 在临床工作台录入患者信息并运行工作流。
- [ ] 2. 查看 `documentation` 字段中的 SOAP 草稿（主观 S / 客观 O / 评估 A / 计划 P）。
- [ ] 3. 确认草稿标注状态为「**待医生签发**」。
- [ ] 4. 医生逐项审核，必要时修改（评估部分使用「疑似/考虑」等措辞，不写明确诊断）。
- [ ] 5. 核对 ICD-10 候选编码，选择最匹配的一项。
- [ ] 6. 确认无误后签发；系统记录修改留痕与签发动作到审计日志。

### 5.2 SOAP 草稿结构

```json
{
  "soap": {
    "subjective": "患者主诉与现病史...",
    "objective": "体格检查 + 生命体征 + 检验结果...",
    "assessment": "临床印象（疑似/考虑...，不含明确诊断）",
    "plan": "拟议计划（待医生签发）"
  },
  "icd10": ["I48.0", "I48.91"]
}
```

### 5.3 ICD-10 编码建议

- 系统给出**多个候选编码**，附 confidence（置信度）。
- **医生必须人工选择**最终编码，不得直接采用 AI 首选。
- 候选编码需经术语层（terminology layer）校验。

### 5.4 修改留痕

所有对草稿的修改、编码选择、签发动作均写入**防篡改审计日志**（HMAC-SHA256 逐条签名，支持密钥轮换），满足 21 CFR Part 11 电子记录要求。

---

## 6. 文献检索与引证

### 6.1 检索流程

1. 在「文献查询」标签页或临床工作台的 `query` 中输入临床问题（如「房颤患者抗凝治疗最新指南」）。
2. LiteratureAgent 调用 PubMed E-utilities 与本地指南库检索。
3. 结果按**证据等级**自动排序返回。

### 6.2 证据等级排序（由高到低）

| 等级 | 说明 |
|---|---|
| 指南（guideline） | NICE / WHO / FDA / CDC / AHA / ACC / ADA / ESC / GINA 等权威机构 |
| 系统综述 / Meta 分析（systematic-review） | 系统性综述与荟萃分析 |
| 随机对照试验（rct） | Randomized Controlled Trial |
| 队列研究（observational） | 队列 / 病例对照 / 横断面研究 |
| 病例报告（case） | 个案报道 |

### 6.3 引证使用

- 每条文献携带 `source` 字段（如 `PMID:12345678`），**可直接点击**跳转原文。
- 在病历中引用时，建议记录 PMID 与证据等级，便于同行追溯。
- 系统仅引用**实际检索到**的文献，绝不编造 PMID 或标题；检索为空时如实报告「未检索到相关证据」。

> 提示：PubMed 客户端未配置时，文献检索不可用，系统会明确告知，不会臆造结果。

---

## 7. 安全与合规须知（医生必读）

### 7.1 AI 输出的法律定位

- DoctorAgent 是**辅助决策工具，非医疗器械诊断产品**。
- 在取得相应资质前，本产品不作为医疗器械销售或使用。
- **医生承担最终临床责任** —— AI 输出不构成诊断或医嘱，任何临床行动须由执业医师签发。

### 7.2 何时不得依赖 AI

出现以下情况，医生应**立即停止依赖 AI 输出**，转入医院标准临床流程：

> ⚠️ - **病情急剧变化**时（如突发意识障碍、循环衰竭），按医院急救 SOP 处理，勿等待 AI 响应。
> - **AI 输出与临床判断严重不符**时，以医生判断为准，并将差异作为不良事件上报。
> - **`requires_human_review = true`** 时，未完成人工复核前不得采信输出。
> - **guardrail 动作为 `block`** 时，输出已被阻断，仅显示警告，禁止使用。

### 7.3 PHI 保护

- 系统在调用任何外部服务（openFDA / RxNorm / PubMed）前，**自动对 18 类 HIPAA Safe Harbor 标识符脱敏**：
  - 10 类核心临床标识：患者姓名、MRN、出生日期、电话、邮箱、SSN、地址、病历号、日期、IP 地址。
  - 8 类扩展标识：传真号、账号、执照号、车辆标识、设备标识、URL、生物标识、面部照片引用。
- 脱敏策略支持 `redact`（替换为 `[REDACTED]`）/ `pseudonymize`（稳定假名）/ `mask`（部分遮蔽）。
- **医生责任**：遵循**最小化原则**，不要在 `query` 自由文本中输入不必要的 PHI（如患者全名、详细住址）。系统虽会脱敏，但应从源头减少风险。

### 7.4 审计与追溯

- **每次查询可追溯**：每个临床决策（规则 → LLM → guardrail → 人工复核标志）均写入审计日志，决策链可完整重建。
- **知识版本可追溯**：临床规则知识库与参考范围均有版本号（当前 v1.1.0），每个决策可追溯到当时的知识版本。
- **修改留痕**：病历草稿的每次修改、编码选择、签发动作均记录在案。
- 角色权限：临床操作按角色（`clinician` / `pharmacist` / `auditor` / `admin`）通过 RBAC + OIDC SSO 控制。

### 7.5 紧急情况与降级

- **LLM 不可用时**：系统自动降级为**纯规则引擎**模式（仍可安全使用）。此时仅确定性规则结果可用（生命体征 / 检验 / DDI / 过敏 / 重复治疗），不产生 LLM 输出，`requires_human_review` 必为 `true`，guardrail 动作为 `flag`。
- **完全故障时**：按医院标准 SOP 处理，不得因系统不可用而延误临床处置。

> ✅ 降级模式是**安全默认**：在未批准 LLM 用于临床的环境，纯规则引擎仍提供用药安全与危急值预警。

---

## 8. 常见问题（FAQ）

| 问题 | 解答 |
|---|---|
| 输出说「需要人工复核」是什么意思？ | `requires_human_review=true`，表示存在阻断级安全发现、子 Agent 被 block/flag、或最终 guardrail 未通过。医生必须完成人工复核后方可采信，禁止直接照搬。 |
| 药物相互作用标红但我觉得没问题，怎么办？ | 标红表示 `contraindicated`/`critical`。医生可在病历中记录**不同意 AI 建议的临床理由**并签发（留痕审计）；但不得绕过复核流程。如确属误报，请按不良事件上报。 |
| 文献检索结果太少？ | 可能因 PubMed 客户端未配置或检索词过窄。尝试调整 `query` 用更通用的临床术语；若客户端未配置，系统会告知「文献检索不可用」，不臆造结果。 |
| AI 生成的病历可以直接用吗？ | **不可以**。所有 SOAP 草稿均标注「待医生签发」，必须经医生审核、修改、签发后方可使用。ICD-10 编码须医生人工选择。 |
| 我能在 query 里输入患者姓名吗？ | **不要**。系统虽会自动脱敏 18 类标识符，但医生应遵循最小化原则，使用 `patient_id` 而非姓名等直接标识符。 |
| LLM 不可用怎么办？ | 系统自动降级为纯规则引擎，仍提供用药安全与危急值预警（无 LLM 输出），`requires_human_review` 必为 `true`，guardrail 动作 `flag`。 |
| 危急值预警会不会漏报？ | 危急值由确定性规则引擎判定（非 LLM），纯逻辑可审计。但医生仍应结合临床判断，规则引擎不替代床旁评估。 |
| 如何追溯某次决策依据的知识版本？ | 审计日志记录每次决策的规则知识库版本（当前 v1.1.0）与参考范围版本，可在「审计日志」标签页查询。 |
| 禁忌级（contraindicated）与危急级（critical）有何区别？ | `contraindicated` 为**禁用**（如过敏交叉反应），须改方案；`critical` 为**危急**（如危急值、major 级 DDI），须立即复核并可能干预。两者均为阻断级，强制人工复核。 |
| 性别会影响参考范围判断吗？ | 会。血红蛋白、血细胞比容、RBC、肌酐、ESR、铁蛋白等项目有性别相关区间。请务必在表单中正确填写 `gender`（`male`/`female`），否则按男性范围评估。 |
| 输出被 `block` 了怎么办？ | guardrail 判 `block` 表示检测到明确诊断措辞、剂量超范围、PHI 泄露或提示注入。此时输出被阻断，仅显示警告。请按警告提示调整输入或改用规则引擎结论，必要时上报。 |
| 跨类过敏（如青霉素用头孢）会报警吗？ | 会。系统支持递归一层跨类匹配：青霉素过敏者使用头孢曲松会命中并标为 `warning`（交叉反应率 1–10%）；造影剂/乳胶/铂类再激发则标为 `critical`。 |

---

## 9. 反馈与支持

### 9.1 临床反馈渠道

- 通过控制台「设置中心」提交功能改进建议。
- 临床准确性问题请附查询的 `patient_id`（脱敏 ID）与时间戳，便于追溯。

### 9.2 不良事件上报流程（重要）

> ⚠️ **AI 误导必须上报**。出现以下情况须按医院不良事件流程上报，并通知系统管理员：

- [ ] AI 输出导致或险些导致临床误判。
- [ ] 危急值/禁忌未正确预警（漏报）。
- [ ] 输出含 PHI 泄露。
- [ ] guardrail 应阻断但未阻断。

上报时请保留：查询时间、`patient_id`、输入快照、输出快照、审计日志条目 ID。

### 9.3 培训资源

- 控制台内置 6 个合成示例预设（safe / drug-interaction / allergy-alert / critical-vitals / critical-labs / duplicate-therapy），建议新用户逐一练习。
- 参考 `docs/CLINICAL_CAPABILITIES.md` 了解能力边界与「不做什么」清单。

### 9.4 每次使用前自检清单

- [ ] 已切换至「医生视图」而非「管理视图」。
- [ ] `patient_id` 使用脱敏 ID，未在 `query` 中输入姓名/住址等不必要 PHI。
- [ ] `gender` 字段已正确填写（影响性别相关参考范围）。
- [ ] 用药与过敏字段完整（支持品牌名与通用名，可用 RxNorm 等价识别）。
- [ ] 已查看 `requires_human_review` 与 `guardrail_result.action`，确认是否需复核。
- [ ] 病历草稿经审核并签发后再使用，ICD-10 编码由医生人工选择。

---

## 10. 术语表（中英对照）

| 术语 | 英文 | 说明 |
|---|---|---|
| SOAP 病历 | SOAP note | 主观(Subjective)/客观(Objective)/评估(Assessment)/计划(Plan) 结构化病历 |
| ICD-10 | International Classification of Diseases, 10th Revision | 国际疾病分类第十版编码 |
| DDI | Drug-Drug Interaction | 药物-药物相互作用 |
| RxCUI | RxNorm Concept Unique Identifier | RxNorm 药物概念唯一标识符 |
| RxNorm | RxNorm | NLM 维护的药物命名规范化体系，支持品牌↔通用名等价 |
| PMID | PubMed ID | PubMed 文献唯一标识符 |
| DOI | Digital Object Identifier | 数字对象标识符 |
| PHI | Protected Health Information | 受保护健康信息 |
| HIPAA | Health Insurance Portability and Accountability Act | 美国健康信息隐私与可携带性法案，定义 18 类 Safe Harbor 标识符 |
| Safe Harbor | Safe Harbor | HIPAA 规定的 18 类去标识化标准 |
| CDS Hooks | Clinical Decision Support Hooks | HL7 临床决策支持钩子标准（patient-view / order-select / order-sign） |
| FHIR | Fast Healthcare Interoperability Resources | HL7 FHIR R4 医疗互操作资源标准 |
| guardrail | Clinical Guardrails | LLM 输出临床护栏（引证/禁忌内容/PHI 泄露/提示注入检测） |
| ReAct | Reasoning + Acting | 推理+行动循环（智能体工具调用范式） |
| fan-out/fan-in | fan-out/fan-in | 多智能体并发分发与汇聚编排模式 |
| DAG | Directed Acyclic Graph | 有向无环图，临床工作流的固定可审计拓扑 |
| SaMD | Software as a Medical Device | 作为医疗器械的软件（FDA 监管框架） |
| 21 CFR Part 11 | 21 CFR Part 11 | FDA 电子记录与电子签名法规 |
| RBAC | Role-Based Access Control | 基于角色的访问控制 |
| OIDC | OpenID Connect | 开放身份认证协议（支持 SSO） |
| 危急值 | critical value | 需立即临床干预的检验/生命体征阈值 |
| 交叉反应 | cross-reactivity | 过敏原与药物跨类反应风险 |
| 重复治疗 | duplicate therapy | 同活性成分重复用药 |
| 降级模式 | degraded mode | LLM 不可用时纯规则引擎运行模式 |

---

> **最终提醒**：本智能体一切输出均为辅助参考，**不替代医生临床判断，需经执业医师签发**。患者安全始终高于效率。
