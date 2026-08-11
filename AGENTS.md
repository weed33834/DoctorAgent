# DoctorAgent 工程指南（Agent System）

## 项目定位

DoctorAgent 是面向医疗机构的**临床决策支持（CDS）智能体**。系统由两层构成：

1. **确定性临床安全引擎**——用药审查、危急值、过敏 / DDI 规则。纯逻辑、无网络依赖、结果优先于 LLM 推断；
2. **LLM / RAG 文献智能体**——基于 ReAct 的多工具智能体，负责医学文献与临床指南的自然语言检索、抽取与问答，所有结论附可追溯引证。

> 文档库（Vault）是上述第二层用于存放医学文献、临床指南与病历资料的知识库。`/vault/*` 系列接口服务于**临床文献检索与问答**，不应被理解为泛化的通用文档管理。

## 临床工作流

- **用药安全审查**：药物相互作用（DDI）、过敏交叉反应、重复用药检测
- **危急值预警**：生命体征（心率 / 血压 / 体温 / SpO₂ / 呼吸频率）与检验指标危急值
- **病历文书**：SOAP 格式病历生成、ICD-10 编码辅助
- **文献检索**：PubMed / 知识库 RAG 检索，所有建议附可追溯引证

## 多智能体协作

- 患者病史 Agent → 用药安全 Agent → 文献 Agent → 文书 Agent（固定 DAG，合规可审计）
- 确定性规则引擎结果优先于 LLM 推断，冲突时以规则为准

## 安全合规

- PHI 脱敏（HIPAA Safe Harbor）、HMAC 审计链、AES-256-GCM 加密
- CDS Hooks 2.0 集成（patient-view / order-select / order-sign）
- FHIR R4 资源读写，SMART-on-FHIR 认证

## CLI 临床命令

```bash
# 执行临床工作流分析（确定性规则引擎 + 可选 LLM 文书）
doctoragent clinical analyze --patient-id P001 \
  --medications "warfarin" "ibuprofen" \
  --allergies "penicillin" \
  --vitals hr=80 sbp=120 dbp=80 \
  --question "患者用药是否安全？"
```

---

## Agent / RAG 子系统（文献与指南检索）

DoctorAgent 的智能体基于 ReAct（Reasoning + Acting）模式，使 LLM 能够通过工具调用、技能执行与 RAG 检索，对医学文献与临床指南进行自然语言交互。该子系统是临床叙事的“文献层”，所有输出应回到临床工作流并被确定性规则校验。

## 架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         User Query（临床问题）                           │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Agent (ReAct Loop)                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                    │
│  │   Think     │→ │   Act       │→ │  Observe    │                    │
│  └─────────────┘  └─────────────┘  └─────────────┘                    │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                ┌───────────────┼───────────────┐
                │               │               │
                ▼               ▼               ▼
        ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
        │    Tools     │ │    Skills    │ │    Memory    │
        │ (文献检索等) │ │ (临床技能)   │ │ (会话记忆)   │
        └──────────────┘ └──────────────┘ └──────────────┘
```

## Components

### 1. 工具系统 (`doctoragent/model/tools.py`)

工具是智能体与外部系统交互的基础单元，使用 JSON Schema 定义，兼容 OpenAI 与 Anthropic function calling API。内置工具面向临床文献场景：

| Tool | Category | Description |
|------|----------|-------------|
| `search_documents` | Retrieval | 用自然语言检索 Vault 中的医学文献 / 指南 |
| `list_files` | Management | 列出 Vault 中文献，支持过滤 |
| `get_file_details` | Management | 获取某篇文献的元信息 |
| `analyze_document` | Analysis | 用 LLM 分析文献内容（如指南要点提取） |
| `compare_documents` | Analysis | 对比多篇指南的推荐差异 |
| `memory` | Memory | 长期记忆的存取 |
| `extract_information` | Extraction | 从文献中抽取结构化信息（如剂量、禁忌） |

#### Tool Definition Format

```python
from doctoragent.model.tools import ToolDefinition, ToolParameter

tool_def = ToolDefinition(
    name="search_documents",
    description="Search clinical literature in the vault",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Natural language search query",
            required=True,
        ),
        ToolParameter(
            name="top_k",
            type="integer",
            description="Number of results to return",
            required=False,
            default=5,
        ),
    ],
    category="retrieval",
)

# Convert to OpenAI API format
openai_tools = tool_def.to_openai_tools()
```

#### Custom Tools

To create a custom tool, extend the `Tool` base class:

```python
from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

class MyCustomTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="my_tool",
            description="My custom tool",
            parameters=[
                ToolParameter(name="input", type="string", description="Input data"),
            ],
        )

    async def execute(self, input: str) -> ToolResult:
        # Implement your tool logic here
        return ToolResult(success=True, data={"result": "processed"})
```

### 2. Agent Framework (`doctoragent/model/agent.py`)

The Agent framework orchestrates the reasoning loop and tool execution.

#### Key Classes

- **`Agent`**: Main agent class implementing the ReAct loop
- **`AgentConfig`**: Configuration for agent behavior
- **`AgentTrajectory`**: Records the execution trajectory
- **`AgentStep`**: Single step in the execution

#### Configuration

```python
from doctoragent.model.agent import AgentConfig

config = AgentConfig(
    max_iterations=10,      # Maximum reasoning iterations
    max_tool_calls=5,       # Maximum tool calls per task
    temperature=0.3,        # Lower temperature for clinical determinism
    enable_planning=True,   # Enable planning phase
    enable_reflection=True, # Enable self-reflection
    safety_mode=True,       # Enable safety guardrails
)
```

#### Usage

```python
from doctoragent.model.agent import create_agent

# Create agent with all default tools
agent = create_agent(
    llm_provider=your_llm_provider,
    rag_pipeline=your_rag_pipeline,
    task_store=your_task_store,
    memory_system=your_memory_system,
)

# Run a clinical literature task
response = agent.run_sync("检索近三年 SGLT2 抑制剂在心衰中的循证证据并总结要点")

# Get execution trajectory
trajectory = agent.get_trajectory()
for step in trajectory.steps:
    print(f"[{step.step_type}] {step.content}")
```

### 3. Skills System (`doctoragent/model/skills.py`)

Skills are high-level task capabilities that combine multiple tools to accomplish complex clinical literature tasks.

#### Built-in Skills

| Skill | Category | Triggers | Description |
|-------|----------|----------|-------------|
| `document_search` | Retrieval | search, find, 找, 搜索 | 检索医学文献 |
| `document_analysis` | Analysis | analyze, summarize, 总结 | 分析文献内容 |
| `document_comparison` | Analysis | compare, difference, 比较 | 对比多篇指南 |
| `information_extraction` | Extraction | extract, get, 提取 | 抽取结构化信息（剂量 / 禁忌） |
| `conversation` | Conversation | remember, recall, 记得 | 多轮对话记忆 |

#### Skill Definition Format

```python
from doctoragent.model.skills import Skill, SkillDefinition, SkillCategory, SkillResult

class MySkill(Skill):
    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="my_skill",
            description="My custom skill",
            category=SkillCategory.ANALYSIS,
            triggers=["analyze", "check", "verify"],
            examples=["Analyze this guideline", "Check for contraindications"],
        )

    def matches(self, query: str) -> bool:
        # Check if query matches skill triggers
        return any(trigger in query.lower() for trigger in self.definition.triggers)

    async def execute(self, query: str, context: dict = None) -> SkillResult:
        # Implement your skill logic here
        return SkillResult(
            success=True,
            skill_name=self.definition.name,
            result={"analysis": "complete"},
        )
```

#### Auto-Matching

The skill registry can automatically find and execute the best matching skill:

```python
from doctoragent.model.skills import create_default_skill_registry

registry = create_default_skill_registry(rag_pipeline=rag)

# Auto-detect and execute skill
result = registry.execute_auto("检索房颤抗凝的两大指南推荐")
```

### 4. RAG Evaluation (`doctoragent/model/evaluation.py`)

The evaluation system measures the quality of RAG responses and agent execution.

#### Metrics

| Metric | Purpose | Threshold |
|--------|---------|-----------|
| Context Precision | Measures if retrieved context is relevant | 0.5 |
| Context Recall | Measures if all relevant documents were retrieved | 0.5 |
| Faithfulness | Measures if the answer is grounded in context | 0.7 |
| Answer Relevancy | Measures if the answer addresses the question | 0.6 |
| Tool Correctness | Measures if the correct tools were called | 0.5 |
| Step Efficiency | Measures if the agent completed the task efficiently | 0.5 |

#### Usage

```python
from doctoragent.model.evaluation import RAGEvaluator, LLMTestCase

evaluator = RAGEvaluator(threshold=0.5)

test_case = LLMTestCase(
    input="华法林与布洛芬合用的出血风险如何管理？",
    actual_output="合用显著增加消化道出血风险，建议……",
    retrieval_context=["Clinical guideline content..."],
)

# Evaluate all metrics
results = evaluator.evaluate(test_case)
for metric_name, result in results.items():
    print(f"{metric_name}: {result.score:.2f} ({'PASS' if result.passed else 'FAIL'})")

# Get overall RAG score
rag_score = evaluator.evaluate_rag_score(test_case)
print(f"Overall RAG score: {rag_score:.2f}")
```

### 5. Context Engineering (`doctoragent/model/rag.py`)

The context engineering system assembles optimal context for LLM responses.

#### Features

- **Token Budget Management**: Ensures context fits within model limits
- **Memory Integration**: Includes relevant memories in context
- **Conversation History**: Maintains multi-turn conversation context
- **Source Citations**: Tracks and includes source references

#### Context Window

```python
from doctoragent.model.rag import ContextEngineer

engineer = ContextEngineer(memory_system=memory)

context = engineer.build_context(
    question="围手术期使用 NSAIDs 的禁忌有哪些？",
    retrieved_chunks=[
        {"text": "Guideline content...", "vault_path": "guideline_nsaids.pdf"},
    ],
    session_id="session123",
    include_memory=True,
)

print(f"System prompt: {len(context.system_prompt)} chars")
print(f"Retrieved context: {len(context.retrieved_context)} chars")
print(f"Total tokens: {context.total_tokens}")
```

## CLI Commands

### `doctoragent ask`

Simple RAG question answering over the clinical literature vault:

```bash
doctoragent ask "围手术期使用 NSAIDs 的禁忌有哪些？"
doctoragent ask "比较 2023 与 2024 版高血压指南的一线用药" --top-k 10
doctoragent ask "上次我们讨论的房颤抗凝方案是什么？" --session-id abc123
```

### `doctoragent agent`

Intelligent agent with tool calling over clinical documents:

```bash
doctoragent agent "检索近三年 SGLT2 抑制剂在心衰中的循证证据并总结要点"
doctoragent agent "对比两份糖尿病指南的筛查建议" --verbose
doctoragent agent "从这批检验报告中抽取所有异常指标" --max-iterations 15
```

## API Endpoints

### POST `/clinical/analyze`

临床工作流分析（用药安全 + 危急值 + 文书），返回确定性规则结果与可追溯引证。

### POST `/vault/ask`

基于 Vault 医学文献的 RAG 问答：

```json
{
  "question": "华法林与布洛芬合用的出血风险如何管理？",
  "top_k": 5,
  "session_id": "optional-session-id",
  "use_memory": true
}
```

Response:

```json
{
  "answer": "合用显著增加消化道出血风险，建议……",
  "sources": [
    {
      "file": "guideline_anticoag.pdf",
      "score": 0.85,
      "content_preview": "Guideline terms..."
    }
  ],
  "model_used": "qwen3:8b",
  "session_id": "abc123"
}
```

### POST `/vault/agent`

面向文献的复杂智能体任务：

```json
{
  "task": "检索并对比房颤抗凝的两大指南推荐",
  "max_iterations": 10,
  "verbose": false
}
```

> 以上接口的根路径为 `/`（生产环境可加 `/api/v1` 前缀）。控制台 UI 挂载于 `/console`，根路径 `/` 重定向至 `/console/`，方便评审直接打开浏览器。

## Testing

Run agent-related tests:

```bash
# Run all agent / RAG tests
python -m pytest tests/ -k "agent or skill or evaluation or rag" -v

# Run specific categories
python -m pytest tests/ -k "Tool" -v
python -m pytest tests/ -k "Agent" -v
python -m pytest tests/ -k "Skill" -v
python -m pytest tests/ -k "Evaluation" -v
```

## Design Principles

1. **确定性优先**：规则引擎结果优先于 LLM，冲突以规则为准
2. **可审计**：所有临床建议附引证，操作进入 HMAC 审计链
3. **模块化**：工具、技能与评估指标相互独立
4. **可扩展**：易于新增临床工具 / 技能 / 指标
5. **可观测**：完整执行轨迹记录
6. **安全**：内置 guardrails 与 PHI 脱敏
7. **评估**：RAG 质量量化指标

## Future Enhancements

> 已落地（不再列入路线图）：并行工具执行、流式响应、Plan-and-Execute、多智能体编排、检查点、MCP 工具互操作、A2A 协议、MCP 客户端、长期记忆整合、语音链路、企业级平台（`doctoragent/enterprise/`）、数据治理目录（`doctoragent/governance/`）、模型比价/成本看板（`model/pricing.py`）、语义缓存（`model/semantic_cache.py`）、错误码体系（`api/error_catalog.py`）、AI 安全威胁库+红队（`security/threat.py`）、Agent 互操作目录+策略（`interop/`）、容灾备份+DR 演练（`disaster/`）、多模态资产库（`multimodal/`）、数据管道（`datapipeline/`）、知识库管理（`knowledge_base.py`）、任务中心（`taskcenter.py`）、用量分析（`/analytics/overview`）、辩论模式（`group_chat.run_debate`）、ADK/AutoGen 适配器、图像生成工具（`tools/image_gen_tool.py`）、压测脚本（`scripts/load_test.py`）、浏览器自动化、群聊编排、K8s 清单 + Grafana 仪表盘 + 评估门禁 + 安全冒烟（`deploy/`、`scripts/`）。

- **Custom Tool Marketplace**: Share and install community tools
- **Agent-to-Agent Negotiation（进阶）**: Cross-agent negotiation / voting beyond the current fan-out/fan-in DAG and A2A task submission
- **Long-Horizon Memory Consolidation（进阶）**: LLM 驱动的跨会话知识图谱提炼（现有 deterministic compaction 之上）
- **企业级长尾（M14 剩余）**: SSO/SAML、离职交接/自助注销、多维成本报表、评论/审批流、发布审核/灰度/市场、业务分析看板、UI i18n、数据驻留/水印
- **性能优化（M23 剩余）**: 提示词精确压缩、自动伸缩（已有语义缓存 + 压测脚本）
- **平台级长尾（M30 若新增）** 按需叠加
