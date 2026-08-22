# 外部组件拼装指南（INTEGRATIONS）

> DoctorAgent 刻意保持零依赖核心。本文档回答一个问题：**如何用成熟开源项目把系统拼装完整**，而不是重复造轮子。
>
> 分三档：`.env` 即插（零代码）→ 微型胶水（≤50 行）→ 结构性改造（需评估）。

---

## 一、`.env` 即插：改配置直连成熟后端

所有 LLM/语音/可观测接口都遵循 OpenAI 兼容协议，因此任何实现该协议的自托管服务都能直接挂上。

### 本地 LLM 推理

| 项目 | 用途 | 配置 |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) | 最简单的本地模型运行时 | `DOCTORAGENT_MODEL__BASE_URL=http://127.0.0.1:11434/v1` |
| [vLLM](https://github.com/vllm-project/vllm) | 生产级高吞吐推理 | 同上，指向 vLLM 端口 |
| [LM Studio](https://lmstudio.ai) / [llama.cpp-server](https://github.com/ggml-org/llama.cpp) / [LocalAI](https://github.com/mudler/LocalAI) | 桌面/边缘场景 | 同上 |

`docker-compose --profile with-llm` 已内置 Ollama 编排。

### 语音链路（ASR/TTS，默认关闭）

| 能力 | 项目 | 配置 |
|---|---|---|
| 语音转文字 | [Speaches](https://github.com/speaches-ai/speaches)（faster-whisper，OpenAI 兼容） | `DOCTORAGENT_VOICE__TRANSCRIBE_BASE_URL` |
| 语音转文字（备选） | [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice) | 同上 |
| 文字转语音 | [openedai-speech](https://github.com/matatonic/openedai-speech)（Piper/CosyVoice 兼容层） | `DOCTORAGENT_VOICE__TTS_BASE_URL` |

### FHIR / EHR 对接

| 项目 | 用途 | 配置 |
|---|---|---|
| [HAPI FHIR](https://github.com/hapifhir/hapi-fhir-jpaserver-starter) | 开源 FHIR R4 服务器（测试/集成） | `DOCTORAGENT_CLINICAL__FHIR_BASE_URL`；compose `--profile with-fhir` |

### 可观测性

| 能力 | 项目 | 配置 |
|---|---|---|
| 追踪 | [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) → [Jaeger](https://www.jaegertracing.io)/[Grafana Tempo](https://grafana.com/oss/tempo/) | `DOCTORAGENT_OTEL_EXPORTER_OTLP_ENDPOINT` + `[server]` extra |
| 指标 | Prometheus（`/metrics` 已内置）+ Grafana | 仪表盘 JSON 在 `deploy/grafana/` |
| LLM 观测 | [Langfuse](https://github.com/langfuse/langfuse) 自托管 | `LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY` |

---

## 二、微型胶水：小改动接大生态

| 目标 | 推荐项目 | 工作量 | 说明 |
|---|---|---|---|
| 远程 Embedding | [TEI (text-embeddings-inference)](https://github.com/huggingface/text-embeddings-inference)、Ollama `/api/embeddings`、[Infinity](https://github.com/michaelfeil/infinity) | ~30 行适配器 | 实现 `embedding.py` 的 `embed()` 抽象即可；医学场景建议 `BAAI/bge-m3` 多语向量 |
| 元搜索引擎 | [SearXNG](https://github.com/searxng/searxng)（开 `format=json`） | ~1 行字段映射 | SearXNG 返回 `content` 而非 `snippet`，在 `general_tools.py` 的自定义搜索后端约定里补一个别名 |
| 评估闭环 | DeepEval（已集成）+ judge 指向本地模型 | 构造参数传 `base_url` | 让评估不依赖云端 API key |

---

## 三、结构性改造：需要立项

| 缺口 | 状态（v0.3.20） | 方案 |
|---|---|---|
| ~~Chroma 向量库未接线~~ | ✅ **已完成**（v0.3.18）：摄入双写 + 查询委托 + 删除同步 + 跨租户过滤，见 `tests/test_vector_backend_wiring.py` | — |
| ~~容器级代码沙箱~~ | ✅ **已完成**（v0.3.20）：Docker/Podman 后端，断网+资源上限+只读根 fs；`DOCTORAGENT_SANDBOX_CONTAINER=1` 启用 | 生产集群可换 gVisor(runsc) runtime 或 E2B |
| PHI 姓名 NER | ✅ **已完成**（v0.3.19）：可选 spaCy 层 `DEID_SPACY_MODEL` | 中文推荐 zh_core_web_sm；生产仍需人工抽检 |
| 数据库 RLS / pgvector | 🔄 迁移进行中：P1 抽象层 ✅（v0.3.22）、P2 会话 ORM 试点 ✅（v0.3.23）、P3a 真机 RLS 实证 + pgvector 冒烟 ✅、P3b pgvector 后端接入检索链 ✅（v0.3.25）；剩余 task_store/memory 铺开与全端点渗透套件 | 设计见 `docs/POSTGRES_MIGRATION.md`；环境见 `deploy/docker-compose.postgres.yml` |

---

## 四、智能体能力完整性对照（2026 标准）

| 能力 | 状态 | 备注 |
|---|---|---|
| ReAct 循环 / 原生 tool calling | ✅ 真实 | 原生 function calling 优先，正则解析兜底 |
| 流式输出（SSE） | ✅ 真实 | chat + vault/agent 双通道 |
| 断点续跑 / 检查点 | ✅ 真实 | `agent/checkpoint.py` |
| 人工介入（HITL） | ✅ 真实 | `model/human_in_loop.py` |
| 固定 DAG 多智能体编排 | ✅ 真实 | 临床管道不可被 LLM 绕过（设计目标） |
| 记忆（短期/情景/语义/程序性） | ✅ 真实 | 含整合压实与遗忘 |
| MCP 客户端+服务端 / A2A | ✅ 真实 | A2A 默认关闭（安全默认） |
| 长期记忆语义召回 | ⚠️ 部分 | facts 用 LIKE 匹配，episodes 有 embedding |
| 全局 wall-clock 限制 | ❌ 缺失 | 仅迭代/工具次数预算，无总时长闸 |
| Anthropic Messages 原生协议 | ❌ 走兼容层 | "anthropic" 平台实际是 OpenAI 格式 |

> 结论：**agent 核心闭环完整可用**。短板集中在"生产加固"（沙箱隔离级、RLS、时长预算）而非"能力缺失"，均可按上表以开源组件补齐。
