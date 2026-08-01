"""Skills system for document management - high-level task capabilities.

Skills are composable units that combine multiple tools to accomplish
complex document management tasks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from doctoragent._utils import async_to_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill Definition
# ---------------------------------------------------------------------------


class SkillCategory(str, Enum):
    """Categories of skills."""

    RETRIEVAL = "retrieval"
    ANALYSIS = "analysis"
    MANAGEMENT = "management"
    EXTRACTION = "extraction"
    CONVERSATION = "conversation"


class SkillDefinition(BaseModel):
    """Definition of a skill."""

    name: str
    description: str
    category: SkillCategory
    triggers: list[str] = Field(default_factory=list)  # Keywords that activate this skill
    examples: list[str] = Field(default_factory=list)

    model_config = ConfigDict(use_enum=True)


class SkillResult(BaseModel):
    """Result from skill execution."""

    success: bool
    skill_name: str
    result: Any = None
    error: str | None = None
    steps_taken: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Skill(ABC):
    """Base class for all skills."""

    @property
    @abstractmethod
    def definition(self) -> SkillDefinition:
        """Return the skill definition."""
        ...

    @abstractmethod
    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute the skill."""
        ...

    def matches(self, query: str) -> bool:
        """Check if this skill matches the query."""
        query_lower = query.lower()
        return any(trigger.lower() in query_lower for trigger in self.definition.triggers)


# ---------------------------------------------------------------------------
# Built-in Skills
# ---------------------------------------------------------------------------


class DocumentSearchSkill(Skill):
    """Search for documents using natural language."""

    def __init__(self, rag_pipeline: Any = None):
        self.rag = rag_pipeline

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="document_search",
            description="Search for documents in the vault using natural language queries",
            category=SkillCategory.RETRIEVAL,
            triggers=["search", "find", "look for", "where is", "找", "搜索", "查找"],
            examples=[
                "Find my contract files",
                "Search for financial reports",
                "Where is my insurance policy?",
            ],
        )

    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute document search."""
        if not self.rag:
            return SkillResult(
                success=False, skill_name=self.definition.name, error="RAG pipeline not initialized"
            )

        try:
            response = self.rag.ask(
                question=query,
                use_memory=True,
                use_query_expansion=True,
            )

            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "answer": response.answer,
                    "sources": [
                        {
                            "file": r.chunk.get("vault_path", "unknown"),
                            "score": r.score,
                            "content_preview": r.chunk.get("text", "")[:200],
                        }
                        for r in response.sources
                    ],
                    "model_used": response.model_used,
                },
                steps_taken=["searched_documents", "ranked_results"],
            )
        except Exception as e:
            return SkillResult(success=False, skill_name=self.definition.name, error=str(e))


class DocumentAnalysisSkill(Skill):
    """Analyze document content."""

    def __init__(self, rag_pipeline: Any = None, llm_provider: Any = None):
        self.rag = rag_pipeline
        self.llm = llm_provider

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="document_analysis",
            description="Analyze documents to extract insights, summaries, or key information",
            category=SkillCategory.ANALYSIS,
            triggers=["analyze", "summarize", "summary", "analyze", "总结", "分析", "摘要"],
            examples=[
                "Summarize my contract",
                "What are the key points in this document?",
                "Analyze the financial report",
            ],
        )

    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute document analysis."""
        if not self.rag:
            return SkillResult(
                success=False, skill_name=self.definition.name, error="RAG pipeline not initialized"
            )

        try:
            # Build analysis prompt
            analysis_prompt = f"""请分析以下内容并提供详细的信息：

用户请求：{query}

请提供：
1. 主要内容摘要
2. 关键要点
3. 重要细节（日期、金额、人名等）
4. 相关建议
"""

            response = self.rag.ask(
                question=analysis_prompt,
                use_memory=True,
                use_query_expansion=False,
            )

            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "analysis": response.answer,
                    "model_used": response.model_used,
                },
                steps_taken=["analyzed_content", "extracted_insights"],
            )
        except Exception as e:
            return SkillResult(success=False, skill_name=self.definition.name, error=str(e))


class DocumentComparisonSkill(Skill):
    """Compare multiple documents."""

    def __init__(self, rag_pipeline: Any = None):
        self.rag = rag_pipeline

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="document_comparison",
            description="Compare multiple documents to find similarities, differences, or conflicts",
            category=SkillCategory.ANALYSIS,
            triggers=["compare", "difference", "similar", "对比", "比较", "差异"],
            examples=[
                "Compare my two contracts",
                "What are the differences between these documents?",
                "Find conflicts in the agreements",
            ],
        )

    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute document comparison."""
        if not self.rag:
            return SkillResult(
                success=False, skill_name=self.definition.name, error="RAG pipeline not initialized"
            )

        try:
            response = self.rag.ask(
                question=f"请比较文档：{query}",
                use_memory=False,
                use_query_expansion=False,
            )

            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "comparison": response.answer,
                    "model_used": response.model_used,
                },
                steps_taken=["retrieved_documents", "compared_content"],
            )
        except Exception as e:
            return SkillResult(success=False, skill_name=self.definition.name, error=str(e))


class InformationExtractionSkill(Skill):
    """Extract structured information from documents."""

    def __init__(self, rag_pipeline: Any = None):
        self.rag = rag_pipeline

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="information_extraction",
            description="Extract specific information like dates, amounts, names from documents",
            category=SkillCategory.EXTRACTION,
            triggers=["extract", "get", "pull out", "提取", "取出", "获取"],
            examples=[
                "Extract all dates from the contract",
                "Get the total amount from the invoice",
                "Who are the parties mentioned?",
            ],
        )

    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute information extraction."""
        if not self.rag:
            return SkillResult(
                success=False, skill_name=self.definition.name, error="RAG pipeline not initialized"
            )

        try:
            extraction_prompt = f"""请从文档中提取以下信息：

用户请求：{query}

请以结构化格式返回提取的信息，包括：
- 提取的项目列表
- 每项的详细信息
- 相关上下文
"""

            response = self.rag.ask(
                question=extraction_prompt,
                use_memory=False,
                use_query_expansion=False,
            )

            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "extracted_info": response.answer,
                    "model_used": response.model_used,
                },
                steps_taken=["parsed_query", "extracted_information"],
            )
        except Exception as e:
            return SkillResult(success=False, skill_name=self.definition.name, error=str(e))


class ConversationSkill(Skill):
    """Handle multi-turn conversations with memory."""

    def __init__(self, memory_system: Any = None):
        self.memory = memory_system

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="conversation",
            description="Handle multi-turn conversations with memory retention",
            category=SkillCategory.CONVERSATION,
            triggers=["remember", "recall", "last time", "之前", "记得", "上次"],
            examples=[
                "Remember that I prefer PDF format",
                "What did we discuss last time?",
                "I told you about my contract before",
            ],
        )

    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute conversation with memory."""
        if not self.memory:
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error="Memory system not initialized",
            )

        try:
            # Check for memory-related queries
            if any(word in query.lower() for word in ["remember", "recall", "上次", "记得"]):
                facts = self.memory.recall_facts(query, limit=5)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result={
                        "memories": [
                            {"content": f.content, "importance": f.importance} for f in facts
                        ],
                        "count": len(facts),
                    },
                    steps_taken=["recalled_memories"],
                )
            else:
                # Store new information
                self.memory.store_fact(query, importance=0.7)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result={"stored": True, "content": query},
                    steps_taken=["stored_memory"],
                )
        except Exception as e:
            return SkillResult(success=False, skill_name=self.definition.name, error=str(e))


# ---------------------------------------------------------------------------
# Skill Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Registry for managing all available skills."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Register a skill."""
        self._skills[skill.definition.name] = skill

    def get(self, name: str) -> Skill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def find_matching_skill(self, query: str) -> Skill | None:
        """Find a skill that matches the query."""
        for skill in self._skills.values():
            if skill.matches(query):
                return skill
        return None

    def list_skills(self) -> list[SkillDefinition]:
        """List all registered skills."""
        return [skill.definition for skill in self._skills.values()]

    async def execute(
        self, skill_name: str, query: str, context: dict[str, Any] | None = None
    ) -> SkillResult:
        """Execute a skill by name."""
        skill = self._skills.get(skill_name)
        if not skill:
            return SkillResult(
                success=False, skill_name=skill_name, error=f"Skill not found: {skill_name}"
            )

        return await skill.execute(query, context)

    def execute_auto(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Auto-detect and execute the best matching skill."""
        skill = self.find_matching_skill(query)
        if not skill:
            return SkillResult(
                success=False,
                skill_name="none",
                error="No matching skill found for query",
            )

        return async_to_sync(skill.execute(query, context), timeout=60)


def create_default_skill_registry(
    rag_pipeline: Any = None,
    memory_system: Any = None,
    llm_provider: Any = None,
) -> SkillRegistry:
    """Create a skill registry with all default skills."""
    registry = SkillRegistry()

    registry.register(DocumentSearchSkill(rag_pipeline))
    registry.register(DocumentAnalysisSkill(rag_pipeline, llm_provider))
    registry.register(DocumentComparisonSkill(rag_pipeline))
    registry.register(InformationExtractionSkill(rag_pipeline))
    registry.register(ConversationSkill(memory_system))

    return registry
