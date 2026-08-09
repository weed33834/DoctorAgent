# Agent System

## 临床AI智能体概览

DoctorAgent 是面向医疗机构的临床决策支持（CDS）智能体，核心能力包括：

### 临床工作流
- **用药安全审查**：检测药物相互作用（DDI）、过敏交叉反应、重复用药
- **危急值预警**：生命体征（心率/血压/体温/SpO2/呼吸频率）与检验指标危急值
- **病历文书**：SOAP 格式病历生成、ICD-10 编码辅助
- **文献检索**：PubMed/知识库 RAG 检索，所有建议附可追溯引证

### 多智能体协作
- 患者病史 Agent → 用药安全 Agent → 文献 Agent → 文书 Agent（固定 DAG，合规可审计）
- 确定性规则引擎结果优先于 LLM 推断，冲突时以规则为准

### 安全合规
- PHI 脱敏（HIPAA Safe Harbor）、HMAC 审计链、AES-256-GCM 加密
- CDS Hooks 2.0 集成（patient-view / order-select / order-sign）
- FHIR R4 资源读写，SMART-on-FHIR 认证

### CLI 临床命令
```bash
# 执行临床工作流分析
doctoragent clinical analyze --patient-id P001 \
  --medications "warfarin" "ibuprofen" \
  --allergies "penicillin" \
  --vitals hr=80 sbp=120 dbp=80 \
  --question "患者用药是否安全？"
```

---

## 辅助功能：文档 Vault

以下为文档 Vault 功能，可用于医学文献、指南文档的本地化管理。

This document describes the intelligent Agent system in DoctorAgent, which enables natural language interaction with your document vault through tool calling, skill execution, and RAG-powered question answering.

## Overview

The Agent system implements the ReAct (Reasoning + Acting) pattern, allowing the LLM to:

1. **Reason** about user requests
2. **Act** by calling appropriate tools
3. **Observe** the results
4. **Iterate** until the task is complete

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         User Query                                      │
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
        └──────────────┘ └──────────────┘ └──────────────┘
```

## Components

### 1. Tool System (`doctoragent/model/tools.py`)

Tools are the building blocks that enable the Agent to interact with external systems. Each tool is defined using JSON Schema format, compatible with OpenAI and Anthropic function calling APIs.

#### Built-in Tools

| Tool | Category | Description |
|------|----------|-------------|
| `search_documents` | Retrieval | Search for documents using natural language queries |
| `list_files` | Management | List files in the vault with optional filtering |
| `get_file_details` | Management | Get detailed information about a specific file |
| `analyze_document` | Analysis | Analyze document content using LLM |
| `compare_documents` | Analysis | Compare multiple documents |
| `memory` | Memory | Store and recall information from long-term memory |
| `extract_information` | Extraction | Extract structured information from documents |

#### Tool Definition Format

```python
from doctoragent.model.tools import ToolDefinition, ToolParameter

tool_def = ToolDefinition(
    name="search_documents",
    description="Search for documents in the vault",
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
    temperature=0.7,        # LLM temperature
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

# Run a task
response = agent.run_sync("Analyze my financial documents")

# Get execution trajectory
trajectory = agent.get_trajectory()
for step in trajectory.steps:
    print(f"[{step.step_type}] {step.content}")
```

### 3. Skills System (`doctoragent/model/skills.py`)

Skills are high-level task capabilities that combine multiple tools to accomplish complex document management tasks.

#### Built-in Skills

| Skill | Category | Triggers | Description |
|-------|----------|----------|-------------|
| `document_search` | Retrieval | search, find, 找, 搜索 | Search for documents |
| `document_analysis` | Analysis | analyze, summarize, 总结 | Analyze document content |
| `document_comparison` | Analysis | compare, difference, 比较 | Compare multiple documents |
| `information_extraction` | Extraction | extract, get, 提取 | Extract structured information |
| `conversation` | Conversation | remember, recall, 记得 | Handle multi-turn conversations |

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
            examples=["Analyze this document", "Check for errors"],
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
result = registry.execute_auto("Search for my contract files")
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
    input="What are the key terms in my contract?",
    actual_output="The contract has a 12-month term with auto-renewal...",
    retrieval_context=["Contract document content..."],
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
    question="What are the key terms?",
    retrieved_chunks=[
        {"text": "Contract content...", "vault_path": "contract.pdf"},
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

Simple RAG question answering:

```bash
doctoragent ask "What are the key terms in my contract?"
doctoragent ask "Summarize my financial documents" --top-k 10
doctoragent ask "What did we discuss last time?" --session-id abc123
```

### `doctoragent agent`

Intelligent agent with tool calling:

```bash
doctoragent agent "Analyze all my contracts and identify key dates"
doctoragent agent "Compare my insurance policies" --verbose
doctoragent agent "Extract all financial figures from my reports" --max-iterations 15
```

## API Endpoints

### POST `/vault/ask`

RAG question answering:

```json
{
  "question": "What are the key terms?",
  "top_k": 5,
  "session_id": "optional-session-id",
  "use_memory": true
}
```

Response:

```json
{
  "answer": "The contract has a 12-month term...",
  "sources": [
    {
      "file": "contract.pdf",
      "score": 0.85,
      "content_preview": "Contract terms..."
    }
  ],
  "model_used": "glm-5.2",
  "session_id": "abc123"
}
```

### POST `/vault/agent`

Agent task execution:

```json
{
  "task": "Analyze my financial documents",
  "max_iterations": 10,
  "verbose": false
}
```

Response:

```json
{
  "answer": "Based on my analysis...",
  "trajectory": [
    {"step": 1, "type": "thought", "content": "Need to search for financial documents"},
    {"step": 2, "type": "action", "tool": "search_documents", "args": {"query": "financial"}},
    {"step": 3, "type": "observation", "content": "Found 5 documents"},
    {"step": 4, "type": "answer", "content": "Analysis complete..."}
  ],
  "tool_calls": 2,
  "execution_time_ms": 1500
}
```

## Testing

Run agent-related tests:

```bash
# Run all agent tests
python -m pytest tests/test_agent.py -v

# Run specific test categories
python -m pytest tests/test_agent.py -k "Tool" -v
python -m pytest tests/test_agent.py -k "Agent" -v
python -m pytest tests/test_agent.py -k "Skill" -v
python -m pytest tests/test_agent.py -k "Evaluation" -v
```

## Design Principles

1. **Modularity**: Tools, skills, and evaluation metrics are independent components
2. **Extensibility**: Easy to add new tools, skills, or metrics
3. **Composability**: Skills combine multiple tools for complex tasks
4. **Observability**: Full execution trajectory recording
5. **Safety**: Built-in guardrails and error handling
6. **Evaluation**: Comprehensive metrics for quality assurance

## Future Enhancements

> 已落地（不再列入路线图）：并行工具执行、流式响应（SSE / WebSocket）、Plan-and-Execute 多步分解、多智能体 orchestrator/worker 协作、检查点持久化、MCP 工具互操作。详见 README「Agent 系统」与 [docs/CLINICAL_CAPABILITIES.md](docs/CLINICAL_CAPABILITIES.md)。

- **Custom Tool Marketplace**: Share and install community tools
- **Agent-to-Agent Negotiation**: Cross-agent negotiation / voting beyond the current fan-out/fan-in DAG
- **Long-Horizon Memory Consolidation**: Periodic episodic → semantic memory compaction
