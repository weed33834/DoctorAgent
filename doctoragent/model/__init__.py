"""Model capability layer - providers, tools, agents, skills, evaluation."""

from doctoragent.model.agent import (
    Agent,
    AgentConfig,
    AgentState,
    AgentTrajectory,
    create_agent,
)
from doctoragent.model.classifier import Classifier
from doctoragent.model.embedding import LocalEmbeddingProvider
from doctoragent.model.evaluation import (
    AgentEvaluator,
    AgentTestCase,
    EvaluationSuite,
    LLMTestCase,
    RAGEvaluator,
)
from doctoragent.model.provider import (
    _PROVIDER_CLASS_MAP,
    BUILT_IN_PROVIDER_NAMES,
    ModelProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    VLLMProvider,
    create_provider,
    detect_platform,
    register_provider,
)
from doctoragent.model.skills import (
    Skill,
    SkillRegistry,
    SkillResult,
    create_default_skill_registry,
)
from doctoragent.model.skills_advanced import (
    ComposedSkill,
    SemanticSkillMatcher,
    SkillComposer,
)
from doctoragent.model.skills_advanced import (
    SkillRegistry as AdvancedSkillRegistry,
)
from doctoragent.model.skills_advanced import (
    create_default_registry as create_advanced_skill_registry,
)
from doctoragent.model.tools import (
    Tool,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    create_default_registry,
)

__all__ = [
    # Providers
    "BUILT_IN_PROVIDER_NAMES",
    "Classifier",
    "LocalEmbeddingProvider",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "VLLMProvider",
    "create_provider",
    "detect_platform",
    "register_provider",
    # Tools
    "Tool",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "create_default_registry",
    # Agent
    "Agent",
    "AgentConfig",
    "AgentState",
    "AgentTrajectory",
    "create_agent",
    # Skills
    "Skill",
    "SkillRegistry",
    "SkillResult",
    "create_default_skill_registry",
    "SemanticSkillMatcher",
    "SkillComposer",
    "ComposedSkill",
    "AdvancedSkillRegistry",
    "create_advanced_skill_registry",
    # Evaluation
    "RAGEvaluator",
    "AgentEvaluator",
    "EvaluationSuite",
    "LLMTestCase",
    "AgentTestCase",
]

for _provider_name in BUILT_IN_PROVIDER_NAMES:
    register_provider(_provider_name, _PROVIDER_CLASS_MAP[_provider_name], allow_override=True)

del _provider_name
