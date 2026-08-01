"""Advanced skills system with semantic matching, composition, and extended built-in skills.

Enhancements over the base skills system:
1. Semantic skill matching: use embeddings/keyword overlap for better matching
2. Skill composition: chain multiple skills into workflows
3. Confidence scoring: rank multiple matching skills
4. Extended built-in skills: lifecycle management, batch operations, security audit,
   sync management, backup/restore, compliance checking
5. Skill persistence: save/load custom skills

The base :mod:`doctoragent.model.skills` module defines the core ``Skill`` /
``SkillDefinition`` / ``SkillResult`` / ``SkillCategory`` primitives and a small
set of retrieval/analysis skills. This module builds on those primitives to
provide:

* :class:`SemanticSkillMatcher` — a TF-IDF + trigger + Jaccard matcher that
  returns ranked ``(skill, confidence)`` pairs instead of the single boolean
  match offered by :meth:`doctoragent.model.skills.Skill.matches`.
* :class:`SkillComposer` / :class:`ComposedSkill` — chain arbitrary skills into
  reusable workflows where each step's output is fed forward as context.
* Seven new built-in skills covering lifecycle, batch, security, sync, backup,
  compliance and knowledge-graph operations.
* :class:`SkillRegistry` — an enhanced registry with semantic matching,
  best-match execution, and JSON persistence (save/load).

All new skills degrade gracefully: when their backing service is not attached
they return a *simulated* / *planned* result rather than raising, which keeps
them useful for routing, dry-runs and testing.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from doctoragent.compat import StrEnum
from doctoragent.model.skills import (
    Skill,
    SkillCategory,
    SkillDefinition,
    SkillResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Operation enums (StrEnum so values serialize cleanly to JSON)
# ---------------------------------------------------------------------------


class LifecycleOperation(StrEnum):
    """Document lifecycle operations understood by :class:`DocumentLifecycleSkill`."""

    CREATE = "create"
    UPDATE = "update"
    ARCHIVE = "archive"
    DELETE = "delete"
    RESTORE = "restore"


class BatchOperationType(StrEnum):
    """Batch operation types understood by :class:`BatchOperationSkill`."""

    CLASSIFY = "classify"
    ENCRYPT = "encrypt"
    EXPORT = "export"
    DELETE = "delete"


class AuditScope(StrEnum):
    """Security audit scopes understood by :class:`SecurityAuditSkill`."""

    SECURITY = "security"
    VULNERABILITY = "vulnerability"
    PERMISSIONS = "permissions"


class SyncAction(StrEnum):
    """Sync actions understood by :class:`SyncManagementSkill`."""

    SYNC = "sync"
    RESOLVE_CONFLICT = "resolve_conflict"
    STATUS = "status"
    MERGE = "merge"


class BackupAction(StrEnum):
    """Backup actions understood by :class:`BackupRestoreSkill`."""

    BACKUP = "backup"
    VERIFY = "verify"
    RESTORE = "restore"


class ComplianceFramework(StrEnum):
    """Compliance frameworks understood by :class:`ComplianceCheckSkill`."""

    GDPR = "gdpr"
    CCPA = "ccpa"
    RETENTION = "retention"
    PRIVACY = "privacy"


# ---------------------------------------------------------------------------
# Semantic skill matching
# ---------------------------------------------------------------------------

# Common English stopwords. CJK single-character tokens are kept regardless.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "my",
        "your",
        "our",
        "their",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "shall",
        "may",
        "might",
        "must",
        "not",
        "no",
        "so",
        "than",
        "too",
        "very",
        "just",
        "about",
        "into",
        "out",
        "up",
        "down",
        "over",
        "under",
        "all",
        "any",
        "some",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
    }
)

# Matches runs of ASCII letters/digits OR a single CJK ideograph.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _is_cjk(ch: str) -> bool:
    """Return True if *ch* is a single CJK ideograph."""
    return len(ch) == 1 and "\u4e00" <= ch <= "\u9fff"


@dataclass
class _SkillDocument:
    """Tokenized text model of a skill used for TF-IDF scoring.

    Built once per skill in :meth:`SemanticSkillMatcher.__init__` and reused
    for every query so that IDF / TF-IDF vectors are not recomputed.
    """

    skill: Skill
    text: str
    tokens: list[str] = dc_field(default_factory=list)
    term_counts: Counter = dc_field(default_factory=Counter)
    tfidf_vector: dict[str, float] = dc_field(default_factory=dict)
    norm: float = 0.0


class SemanticSkillMatcher:
    """Rank skills against a natural-language query using confidence scores.

    The confidence score blends three signals:

    * **Trigger matching** (weight ``0.45``) — the fraction of a skill's
      declared triggers that appear as substrings of the query. This mirrors
      the behaviour of :meth:`doctoragent.model.skills.Skill.matches` but is
      graded rather than boolean.
    * **TF-IDF cosine similarity** (weight ``0.35``) — a lightweight
      bag-of-words model built from each skill's name, description, triggers
      and examples. IDF is computed across the registered skill corpus.
    * **Jaccard similarity** (weight ``0.20``) — set overlap between the
      query's tokens and the skill's tokens.

    When all three signals are zero the matcher falls back to substring
    matching against the skill name and description so that obvious matches
    are never missed entirely.
    """

    def __init__(self, skills: list[Skill]) -> None:
        self._skills: list[Skill] = list(skills)
        self._docs: list[_SkillDocument] = []
        for skill in self._skills:
            text = self._skill_text(skill)
            tokens = self._tokenize(text)
            self._docs.append(
                _SkillDocument(
                    skill=skill,
                    text=text,
                    tokens=tokens,
                    term_counts=Counter(tokens),
                )
            )
        self._doc_by_id: dict[int, _SkillDocument] = {id(d.skill): d for d in self._docs}
        self._idf: dict[str, float] = self._compute_idf()
        # Pre-compute each document's TF-IDF vector and L2 norm.
        for doc in self._docs:
            doc.tfidf_vector = self._build_tfidf_vector(doc.term_counts)
            doc.norm = self._vector_norm(doc.tfidf_vector)

    # -- public API --------------------------------------------------------

    def match(self, query: str, top_k: int = 3) -> list[tuple[Skill, float]]:
        """Return the top-*top_k* skills ranked by descending confidence.

        Only skills with a strictly positive confidence are returned; if no
        skill matches at all an empty list is returned.
        """
        if not query or not self._skills:
            return []
        scored: list[tuple[Skill, float]] = []
        for doc in self._docs:
            score = self._calculate_confidence(query, doc.skill)
            if score > 0.0:
                scored.append((doc.skill, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        if top_k <= 0:
            return scored
        return scored[:top_k]

    def _calculate_confidence(self, query: str, skill: Skill) -> float:
        """Calculate a match confidence in the inclusive range ``[0, 1]``."""
        query_lower = query.lower()

        # 1. Trigger substring matching (strong signal).
        triggers = skill.definition.triggers
        if triggers:
            hits = sum(1 for t in triggers if t.lower() in query_lower)
            trigger_score = hits / len(triggers)
        else:
            trigger_score = 0.0

        # 2. TF-IDF cosine similarity.
        tfidf_score = self._tfidf_cosine(query, skill)

        # 3. Jaccard similarity on keyword token sets.
        query_tokens = set(self._tokenize(query))
        skill_tokens = self._skill_tokens(skill)
        jaccard_score = self._jaccard_similarity(query_tokens, skill_tokens)

        confidence = 0.45 * trigger_score + 0.35 * tfidf_score + 0.20 * jaccard_score

        # Fallback: substring matching when no semantic overlap at all.
        if confidence == 0.0:
            name = skill.definition.name.lower().replace("_", " ")
            desc_tokens = set(self._tokenize(skill.definition.description))
            if name in query_lower or any(tok in query_tokens for tok in desc_tokens):
                confidence = 0.1

        return min(confidence, 1.0)

    # -- tokenization & similarity helpers ---------------------------------

    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization that handles English and CJK text.

        ASCII letter/digit runs are kept as single tokens (stop-filtered,
        minimum length 2). Individual CJK ideographs are always kept because
        a single Chinese character often carries a full word's meaning.
        """
        if not text:
            return []
        lowered = text.lower()
        raw = _TOKEN_RE.findall(lowered)
        tokens: list[str] = []
        for tok in raw:
            if tok in _STOPWORDS:
                continue
            if len(tok) == 1 and not _is_cjk(tok):
                continue
            tokens.append(tok)
        return tokens

    @staticmethod
    def _jaccard_similarity(set1: set[str], set2: set[str]) -> float:
        """Jaccard similarity between two token sets, in ``[0, 1]``."""
        if not set1 and not set2:
            return 0.0
        union = set1 | set2
        if not union:
            return 0.0
        return len(set1 & set2) / len(union)

    # -- TF-IDF machinery --------------------------------------------------

    def _skill_text(self, skill: Skill) -> str:
        """Flatten a skill's definition into a single searchable text blob."""
        definition = skill.definition
        parts: list[str] = [definition.name.replace("_", " "), definition.description]
        parts.extend(definition.triggers)
        parts.extend(definition.examples)
        return " ".join(parts)

    def _skill_tokens(self, skill: Skill) -> set[str]:
        """Return the cached token set for *skill* (rebuilt if unknown)."""
        doc = self._doc_by_id.get(id(skill))
        if doc is not None:
            return set(doc.tokens)
        return set(self._tokenize(self._skill_text(skill)))

    def _compute_idf(self) -> dict[str, float]:
        """Compute smoothed inverse document frequency for every term."""
        n_docs = len(self._docs)
        df: Counter[str] = Counter()
        for doc in self._docs:
            for term in doc.term_counts:
                df[term] += 1
        # Smoothed IDF so unseen-but-possible terms are never negative and
        # a term appearing in every document does not collapse to zero.
        return {term: math.log((n_docs + 1) / (freq + 1)) + 1.0 for term, freq in df.items()}

    def _build_tfidf_vector(self, counts: Counter) -> dict[str, float]:
        """Build a TF-IDF vector from raw term counts."""
        total = sum(counts.values())
        if total == 0:
            return {}
        vector: dict[str, float] = {}
        for term, count in counts.items():
            tf = count / total
            idf = self._idf.get(term, 0.0)
            weight = tf * idf
            if weight != 0.0:
                vector[term] = weight
        return vector

    @staticmethod
    def _vector_norm(vector: dict[str, float]) -> float:
        """L2 norm of a sparse TF-IDF vector."""
        return math.sqrt(sum(weight * weight for weight in vector.values()))

    def _tfidf_cosine(self, query: str, skill: Skill) -> float:
        """Cosine similarity between the query and a skill's TF-IDF vector."""
        doc = self._doc_by_id.get(id(skill))
        if doc is None or doc.norm == 0.0:
            return 0.0
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return 0.0
        q_counts = Counter(query_tokens)
        q_total = sum(q_counts.values())
        q_vector: dict[str, float] = {}
        for term, count in q_counts.items():
            idf = self._idf.get(term, 0.0)
            if idf <= 0.0:
                continue
            q_vector[term] = (count / q_total) * idf
        if not q_vector:
            return 0.0
        q_norm = self._vector_norm(q_vector)
        if q_norm == 0.0:
            return 0.0
        # Dot product over shared terms; iterate the smaller vector.
        if len(q_vector) <= len(doc.tfidf_vector):
            small, large = q_vector, doc.tfidf_vector
        else:
            small, large = doc.tfidf_vector, q_vector
        dot = 0.0
        for term, weight in small.items():
            other = large.get(term)
            if other:
                dot += weight * other
        return dot / (q_norm * doc.norm)


# ---------------------------------------------------------------------------
# Skill composition
# ---------------------------------------------------------------------------


class ComposedSkill(Skill):
    """A skill that chains multiple sub-skills into a sequential workflow.

    Each sub-skill is executed in order. The :attr:`SkillResult.result` of one
    step is injected into the next step's context under the
    ``previous_result`` key (along with ``previous_skill`` and an accumulating
    ``step_results`` list), so downstream skills can build on upstream
    output. Execution stops at the first failing step.
    """

    def __init__(self, name: str, description: str, skills: list[Skill]) -> None:
        self._name = name
        self._description = description
        self._skills: list[Skill] = list(skills)

    @property
    def definition(self) -> SkillDefinition:
        """Return a definition that combines the triggers/examples of sub-skills."""
        triggers: list[str] = []
        examples: list[str] = []
        for skill in self._skills:
            for trigger in skill.definition.triggers:
                if trigger not in triggers:
                    triggers.append(trigger)
            for example in skill.definition.examples:
                if example not in examples:
                    examples.append(example)
        if self._skills:
            category = self._skills[0].definition.category
        else:
            category = SkillCategory.MANAGEMENT
        return SkillDefinition(
            name=self._name,
            description=self._description,
            category=category,
            triggers=triggers,
            examples=examples,
        )

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Execute sub-skills in sequence, threading output forward as context."""
        ctx: dict[str, Any] = dict(context or {})
        steps_taken: list[str] = []
        composed_of = [skill.definition.name for skill in self._skills]
        last_result: SkillResult | None = None

        for skill in self._skills:
            try:
                result = await skill.execute(query, ctx)
            except Exception as exc:  # noqa: BLE001 — surface as SkillResult
                logger.exception("Composed skill step '%s' raised", skill.definition.name)
                steps_taken.append(f"{skill.definition.name}: error ({exc})")
                return SkillResult(
                    success=False,
                    skill_name=self.definition.name,
                    error=f"Step '{skill.definition.name}' raised: {exc}",
                    steps_taken=steps_taken,
                    metadata={"composed_of": composed_of, "failed_at": skill.definition.name},
                )
            status = "ok" if result.success else f"failed ({result.error})"
            steps_taken.append(f"{skill.definition.name}: {status}")
            if not result.success:
                return SkillResult(
                    success=False,
                    skill_name=self.definition.name,
                    error=result.error,
                    steps_taken=steps_taken,
                    metadata={"composed_of": composed_of, "failed_at": skill.definition.name},
                )
            # Thread the output forward so the next step can consume it.
            ctx["previous_result"] = result.result
            ctx["previous_skill"] = skill.definition.name
            ctx.setdefault("step_results", []).append(
                {"skill": skill.definition.name, "result": result.result}
            )
            last_result = result

        return SkillResult(
            success=True,
            skill_name=self.definition.name,
            result=last_result.result if last_result is not None else None,
            steps_taken=steps_taken,
            metadata={"composed_of": composed_of},
        )


class SkillComposer:
    """Compose multiple skills into reusable named workflows."""

    def compose(
        self,
        skills: list[Skill],
        name: str,
        description: str,
    ) -> ComposedSkill:
        """Compose *skills* into a single :class:`ComposedSkill`."""
        return ComposedSkill(name, description, skills)

    def create_workflow(self, steps: list[tuple[Skill, str]]) -> ComposedSkill:
        """Create a workflow from ``(skill, step_description)`` pairs.

        The resulting :class:`ComposedSkill` is named after its constituent
        skills and its description joins the per-step descriptions with
        `` -> `` to express the pipeline.
        """
        skills = [skill for skill, _ in steps]
        descriptions = [desc for _, desc in steps]
        name = "workflow_" + "_".join(skill.definition.name for skill in skills)
        if len(name) > 80:
            name = name[:77] + "..."
        combined_description = " -> ".join(descriptions) if descriptions else "Composed workflow"
        return self.compose(skills, name, combined_description)


# ---------------------------------------------------------------------------
# Extended built-in skills
# ---------------------------------------------------------------------------


def _to_dict(obj: Any) -> Any:
    """Best-effort serialization of a domain object to a JSON-safe dict."""
    if obj is None:
        return None
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return obj


def _extract_quoted_or_labelled(query: str, labels: tuple[str, ...]) -> str | None:
    """Extract a target phrase following any of *labels* or from quotes."""
    for label in labels:
        pattern = rf"{re.escape(label)}\s+['\"]?(.+?)['\"]?(?:$|[\.,;\?])"
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    quoted = re.search(r"['\"]([^'\"]+)['\"]", query)
    if quoted:
        return quoted.group(1).strip()
    return None


class DocumentLifecycleSkill(Skill):
    """Manage the document lifecycle: create, update, archive, delete, restore."""

    _OPERATION_KEYWORDS: dict[LifecycleOperation, tuple[str, ...]] = {
        LifecycleOperation.CREATE: ("create", "new", "add", "新建", "创建"),
        LifecycleOperation.UPDATE: ("update", "edit", "modify", "修改", "更新"),
        LifecycleOperation.ARCHIVE: ("archive", "archived", "归档"),
        LifecycleOperation.DELETE: ("delete", "remove", "trash", "删除"),
        LifecycleOperation.RESTORE: ("restore", "recover", "undelete", "恢复"),
    }

    def __init__(self, vault_manager: Any = None, task_store: Any = None) -> None:
        self.vault = vault_manager
        self.task_store = task_store

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="document_lifecycle",
            description="Manage document lifecycle: create, update, archive, delete, and restore documents",
            category=SkillCategory.MANAGEMENT,
            triggers=["create document", "archive", "delete document", "restore", "update file"],
            examples=[
                "Create a new document titled Meeting Notes",
                "Archive the old invoices",
                "Delete the draft report",
                "Restore the deleted contract",
                "Update the project plan",
            ],
        )

    def _parse_operation(self, query: str) -> LifecycleOperation | None:
        query_lower = query.lower()
        for operation, keywords in self._OPERATION_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return operation
        return None

    def _parse_target(self, query: str) -> str | None:
        return _extract_quoted_or_labelled(query, ("titled", "named", "called"))

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Parse the lifecycle intent and execute (or plan) the operation."""
        operation = self._parse_operation(query)
        if operation is None:
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error="Could not determine lifecycle operation from query",
            )
        target = self._parse_target(query)
        steps_taken = [f"parsed_operation:{operation}", f"target:{target or 'unspecified'}"]
        try:
            if self.vault is not None:
                result = self._execute_with_vault(operation, target)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result=result,
                    steps_taken=steps_taken + ["executed"],
                    metadata={"operation": operation, "target": target},
                )
            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "operation": operation,
                    "target": target,
                    "status": "planned",
                    "message": f"Would {operation} document '{target or 'unspecified'}'",
                },
                steps_taken=steps_taken,
                metadata={"operation": operation, "target": target, "simulated": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Document lifecycle operation failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )

    def _execute_with_vault(
        self, operation: LifecycleOperation, target: str | None
    ) -> dict[str, Any]:
        """Delegate to the vault manager when one is attached."""
        method = getattr(self.vault, str(operation), None) or getattr(
            self.vault, f"{operation}_document", None
        )
        if not callable(method):
            return {
                "operation": operation,
                "target": target,
                "status": "unsupported",
                "message": f"Vault manager does not support '{operation}'",
            }
        output = method(target) if target else method()
        return {"operation": operation, "target": target, "status": "executed", "output": output}


class BatchOperationSkill(Skill):
    """Batch operations: classify, encrypt, export, or delete many files at once."""

    _TYPE_KEYWORDS: dict[BatchOperationType, tuple[str, ...]] = {
        BatchOperationType.CLASSIFY: ("classify", "classification", "categorize", "分类"),
        BatchOperationType.ENCRYPT: ("encrypt", "encryption", "加密"),
        BatchOperationType.EXPORT: ("export", "导出"),
        BatchOperationType.DELETE: ("delete", "remove", "删除"),
    }

    def __init__(
        self,
        vault_manager: Any = None,
        classifier: Any = None,
        encryptor: Any = None,
    ) -> None:
        self.vault = vault_manager
        self.classifier = classifier
        self.encryptor = encryptor

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="batch_operation",
            description="Perform batch operations: classify, encrypt, export, or delete multiple files at once",
            category=SkillCategory.MANAGEMENT,
            triggers=["batch", "bulk", "multiple files", "all files"],
            examples=[
                "Batch classify all files in the invoices folder",
                "Bulk encrypt all PDFs",
                "Export all files tagged as contracts",
                "Delete all archived files older than 2023",
            ],
        )

    def _parse_type(self, query: str) -> BatchOperationType | None:
        query_lower = query.lower()
        for op_type, keywords in self._TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return op_type
        return None

    def _parse_scope(self, query: str) -> str | None:
        if "all files" in query.lower():
            return "all"
        match = re.search(
            r"(?:in|from|under)\s+(?:the\s+)?([^,\.;]+?)(?:\s+folder)?(?:$|[\.,;])",
            query,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        return None

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Parse the batch intent and execute (or plan) the operation."""
        op_type = self._parse_type(query)
        if op_type is None:
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error="Could not determine batch operation type",
            )
        scope = self._parse_scope(query)
        steps_taken = [f"parsed_type:{op_type}", f"scope:{scope or 'all'}"]
        try:
            if self._has_backend(op_type):
                result = self._execute_batch(op_type, scope)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result=result,
                    steps_taken=steps_taken + ["executed"],
                    metadata={"operation": op_type, "scope": scope},
                )
            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "operation": op_type,
                    "scope": scope,
                    "status": "planned",
                    "message": f"Would batch {op_type} files in scope '{scope or 'all'}'",
                },
                steps_taken=steps_taken,
                metadata={"operation": op_type, "scope": scope, "simulated": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Batch operation failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )

    def _has_backend(self, op_type: BatchOperationType) -> bool:
        if op_type == BatchOperationType.CLASSIFY:
            return self.classifier is not None
        if op_type == BatchOperationType.ENCRYPT:
            return self.encryptor is not None
        return self.vault is not None

    def _select_backend(self, op_type: BatchOperationType) -> Any:
        if op_type == BatchOperationType.CLASSIFY:
            return self.classifier
        if op_type == BatchOperationType.ENCRYPT:
            return self.encryptor
        return self.vault

    def _execute_batch(
        self,
        op_type: BatchOperationType,
        scope: str | None,
    ) -> dict[str, Any]:
        backend = self._select_backend(op_type)
        if backend is None:
            return {
                "operation": op_type,
                "scope": scope,
                "status": "unsupported",
                "message": "No backend available for this batch operation",
            }
        method = getattr(backend, f"batch_{op_type}", None) or getattr(backend, str(op_type), None)
        if not callable(method):
            return {
                "operation": op_type,
                "scope": scope,
                "status": "unsupported",
                "message": f"Backend does not support batch '{op_type}'",
            }
        output = method(scope) if scope else method()
        return {"operation": op_type, "scope": scope, "status": "executed", "output": output}


class SecurityAuditSkill(Skill):
    """Security scanning, vulnerability detection, and permission review."""

    _SCOPE_KEYWORDS: dict[AuditScope, tuple[str, ...]] = {
        AuditScope.VULNERABILITY: ("vulnerability", "vulnerable", "cve", "漏洞"),
        AuditScope.PERMISSIONS: ("permission", "permissions", "access control", "acl", "权限"),
    }

    def __init__(self, vault_manager: Any = None, security_scanner: Any = None) -> None:
        self.vault = vault_manager
        self.scanner = security_scanner

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="security_audit",
            description="Run security scans, detect vulnerabilities, and review document permissions",
            category=SkillCategory.ANALYSIS,
            triggers=["security scan", "audit", "vulnerability", "permission check"],
            examples=[
                "Run a security scan on the vault",
                "Audit permissions for all documents",
                "Check for vulnerabilities in encrypted files",
                "Review access control for the contracts folder",
            ],
        )

    def _parse_scope(self, query: str) -> AuditScope:
        query_lower = query.lower()
        for scope, keywords in self._SCOPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return scope
        return AuditScope.SECURITY

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Run (or simulate) a security audit for the parsed scope."""
        scope = self._parse_scope(query)
        steps_taken = [f"scope:{scope}"]
        try:
            if self.scanner is not None:
                findings = self._run_scan(scope)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result=findings,
                    steps_taken=steps_taken + ["scanned"],
                    metadata={"scope": scope},
                )
            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result=self._simulated_findings(scope),
                steps_taken=steps_taken,
                metadata={"scope": scope, "simulated": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Security audit failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )

    def _run_scan(self, scope: AuditScope) -> dict[str, Any]:
        method = getattr(self.scanner, f"scan_{scope}", None) or getattr(self.scanner, "scan", None)
        if not callable(method):
            return {
                "scope": scope,
                "status": "unsupported",
                "message": f"Scanner does not support scope '{scope}'",
            }
        output = method()
        return {"scope": scope, "status": "executed", "findings": output}

    def _simulated_findings(self, scope: AuditScope) -> dict[str, Any]:
        return {
            "scope": scope,
            "status": "simulated",
            "summary": f"Simulated {scope} audit completed",
            "findings": [],
            "recommendations": [
                "Review document access permissions regularly",
                "Ensure sensitive documents are encrypted at rest",
                "Audit API keys and shared credentials",
                "Enable access logging for sensitive folders",
            ],
        }


class SyncManagementSkill(Skill):
    """Trigger sync, resolve conflicts, view sync status, and merge changes."""

    _ACTION_KEYWORDS: dict[SyncAction, tuple[str, ...]] = {
        SyncAction.RESOLVE_CONFLICT: ("conflict", "resolve", "冲突", "解决"),
        SyncAction.STATUS: ("status", "state", "状态"),
        SyncAction.MERGE: ("merge", "合并"),
    }

    def __init__(self, sync_engine: Any = None) -> None:
        self.sync = sync_engine

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="sync_management",
            description="Trigger synchronization, resolve conflicts, and view sync status across devices",
            category=SkillCategory.MANAGEMENT,
            triggers=["sync", "synchronize", "conflict", "merge"],
            examples=[
                "Sync my vault now",
                "Resolve sync conflicts",
                "Show sync status",
                "Merge changes from the remote",
            ],
        )

    def _parse_action(self, query: str) -> SyncAction:
        query_lower = query.lower()
        for action, keywords in self._ACTION_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return action
        return SyncAction.SYNC

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Manage (or simulate) the parsed sync action."""
        action = self._parse_action(query)
        steps_taken = [f"action:{action}"]
        try:
            if self.sync is not None:
                result = self._run_sync(action)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result=result,
                    steps_taken=steps_taken + ["executed"],
                    metadata={"action": action},
                )
            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "action": action,
                    "status": "simulated",
                    "message": f"Would perform {action} (no sync engine attached)",
                },
                steps_taken=steps_taken,
                metadata={"action": action, "simulated": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync management failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )

    def _run_sync(self, action: SyncAction) -> dict[str, Any]:
        method = getattr(self.sync, str(action), None) or getattr(self.sync, f"{action}_sync", None)
        if not callable(method):
            return {
                "action": action,
                "status": "unsupported",
                "message": f"Sync engine does not support action '{action}'",
            }
        output = method()
        return {"action": action, "status": "executed", "output": output}


class BackupRestoreSkill(Skill):
    """Create backups, verify backup integrity, and restore from backup."""

    _ACTION_KEYWORDS: dict[BackupAction, tuple[str, ...]] = {
        BackupAction.VERIFY: ("verify", "check backup", "integrity", "验证"),
        BackupAction.RESTORE: ("restore", "recover", "recovery", "恢复"),
    }

    def __init__(self, backup_manager: Any = None) -> None:
        self.backup = backup_manager

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="backup_restore",
            description="Create backups, verify backup integrity, and restore documents from backup",
            category=SkillCategory.MANAGEMENT,
            triggers=["backup", "restore", "recovery"],
            examples=[
                "Create a full backup of the vault",
                "Verify the latest backup",
                "Restore documents from yesterday's backup",
                "Recover deleted files from backup",
            ],
        )

    def _parse_action(self, query: str) -> BackupAction:
        query_lower = query.lower()
        for action, keywords in self._ACTION_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return action
        return BackupAction.BACKUP

    def _parse_snapshot(self, query: str) -> str | None:
        match = re.search(
            r"(?:from|of)\s+(?:the\s+)?(?:latest\s+|yesterday'?s\s+|today'?s\s+)?"
            r"([a-zA-Z0-9_\-:\s]+?)(?:\s+backup)?(?:$|[\.,;])",
            query,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        return None

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Manage (or simulate) the parsed backup/restore action."""
        action = self._parse_action(query)
        snapshot = self._parse_snapshot(query)
        steps_taken = [f"action:{action}", f"snapshot:{snapshot or 'latest'}"]
        try:
            if self.backup is not None:
                result = self._run_backup(action, snapshot)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result=result,
                    steps_taken=steps_taken + ["executed"],
                    metadata={"action": action, "snapshot": snapshot},
                )
            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result={
                    "action": action,
                    "snapshot": snapshot,
                    "status": "simulated",
                    "message": f"Would {action} from snapshot '{snapshot or 'latest'}'",
                },
                steps_taken=steps_taken,
                metadata={"action": action, "snapshot": snapshot, "simulated": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backup/restore failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )

    def _run_backup(self, action: BackupAction, snapshot: str | None) -> dict[str, Any]:
        method = getattr(self.backup, str(action), None) or getattr(
            self.backup, f"{action}_backup", None
        )
        if not callable(method):
            return {
                "action": action,
                "snapshot": snapshot,
                "status": "unsupported",
                "message": f"Backup manager does not support action '{action}'",
            }
        # Backup creates a new snapshot; verify/restore target an existing one.
        if action == BackupAction.BACKUP or not snapshot:
            output = method()
        else:
            output = method(snapshot)
        return {"action": action, "snapshot": snapshot, "status": "executed", "output": output}


class ComplianceCheckSkill(Skill):
    """GDPR/CCPA compliance, data subject requests, and retention policies."""

    _FRAMEWORK_KEYWORDS: dict[ComplianceFramework, tuple[str, ...]] = {
        ComplianceFramework.GDPR: ("gdpr", "general data protection"),
        ComplianceFramework.CCPA: ("ccpa", "california consumer privacy"),
        ComplianceFramework.RETENTION: ("retention", "retain", "保留", "保存期限"),
        ComplianceFramework.PRIVACY: ("privacy", "data subject", "personal data", "隐私"),
    }

    def __init__(self, vault_manager: Any = None, compliance_engine: Any = None) -> None:
        self.vault = vault_manager
        self.engine = compliance_engine

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="compliance_check",
            description="Check GDPR/CCPA compliance, handle data subject requests, and enforce retention policies",
            category=SkillCategory.ANALYSIS,
            triggers=["gdpr", "compliance", "data subject", "retention", "privacy"],
            examples=[
                "Check GDPR compliance for the vault",
                "Process a data subject access request",
                "Review retention policies for financial documents",
                "Is my vault CCPA compliant?",
            ],
        )

    def _parse_framework(self, query: str) -> ComplianceFramework | None:
        query_lower = query.lower()
        for framework, keywords in self._FRAMEWORK_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return framework
        return None

    def _parse_request_type(self, query: str) -> str:
        query_lower = query.lower()
        if (
            "erasure" in query_lower
            or "deletion request" in query_lower
            or "forgotten" in query_lower
        ):
            return "data_subject_erasure"
        if "access request" in query_lower or "data subject" in query_lower:
            return "data_subject_access"
        if "retention" in query_lower:
            return "retention_review"
        return "compliance_check"

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Run (or simulate) the parsed compliance check."""
        framework = self._parse_framework(query)
        request_type = self._parse_request_type(query)
        steps_taken = [f"framework:{framework or 'general'}", f"request:{request_type}"]
        try:
            if self.engine is not None:
                result = self._run_compliance(framework, request_type)
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result=result,
                    steps_taken=steps_taken + ["executed"],
                    metadata={"framework": framework, "request_type": request_type},
                )
            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result=self._simulated_report(framework, request_type),
                steps_taken=steps_taken,
                metadata={"framework": framework, "request_type": request_type, "simulated": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Compliance check failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )

    def _run_compliance(
        self,
        framework: ComplianceFramework | None,
        request_type: str,
    ) -> dict[str, Any]:
        method = getattr(self.engine, request_type, None) or getattr(self.engine, "check", None)
        if not callable(method):
            return {
                "framework": framework,
                "request_type": request_type,
                "status": "unsupported",
                "message": f"Compliance engine does not support '{request_type}'",
            }
        output = method(framework) if framework else method()
        return {
            "framework": framework,
            "request_type": request_type,
            "status": "executed",
            "output": output,
        }

    def _simulated_report(
        self,
        framework: ComplianceFramework | None,
        request_type: str,
    ) -> dict[str, Any]:
        return {
            "framework": framework,
            "request_type": request_type,
            "status": "simulated",
            "summary": f"Simulated {framework or 'general'} compliance {request_type}",
            "findings": [],
            "recommendations": [
                "Maintain a data inventory mapping personal data to documents",
                "Define and enforce document retention schedules",
                "Provide mechanisms for data subject access and erasure",
                "Log access to personal data for audit trails",
            ],
        }


class KnowledgeGraphQuerySkill(Skill):
    """Query the knowledge graph: find relationships, entities, and connections.

    When a :class:`doctoragent.model.knowledge_graph.KnowledgeGraph` instance is
    attached this skill performs real graph traversal (entity lookup,
    subgraph expansion and relation listing) and falls back to graph-based
    retrieval. Without a graph it returns a simulated result so routing still
    works.
    """

    def __init__(self, knowledge_graph: Any = None, llm_provider: Any = None) -> None:
        self.graph = knowledge_graph
        self.llm = llm_provider

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="knowledge_graph_query",
            description="Query the knowledge graph to find relationships, entities, and connections between concepts",
            category=SkillCategory.RETRIEVAL,
            triggers=["relationship", "connection", "related to", "entity", "graph"],
            examples=[
                "How is Alice related to Project X?",
                "Find connections between these entities",
                "Show the relationship graph for the contract",
                "What entities are connected to the vendor?",
            ],
        )

    def _extract_entity(self, query: str) -> str | None:
        """Heuristically extract the seed entity name from the query."""
        for label in ("related to", "connected to", "connections to", "linked to"):
            match = re.search(
                rf"{re.escape(label)}\s+['\"]?(.+?)['\"]?(?:$|[\?\.,;])",
                query,
                re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()
        match = re.search(r"connection(?:s)? between\s+(.+)", query, re.IGNORECASE)
        if match:
            # "between A and B" — seed with the first entity.
            return match.group(1).strip().split(" and ")[0].strip()
        return _extract_quoted_or_labelled(query, ("entity", "for"))

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Query (or simulate) the knowledge graph for relationships."""
        steps_taken: list[str] = []
        try:
            if self.graph is None:
                return SkillResult(
                    success=True,
                    skill_name=self.definition.name,
                    result={
                        "status": "simulated",
                        "message": "No knowledge graph attached; cannot query relationships",
                    },
                    steps_taken=["checked_graph"],
                    metadata={"simulated": True},
                )

            entity = self._extract_entity(query)
            steps_taken.append(f"extracted_entity:{entity or 'none'}")
            results: dict[str, Any] = {}

            if entity:
                get_entity = getattr(self.graph, "get_entity", None)
                ent = get_entity(entity) if callable(get_entity) else None
                if ent is not None:
                    results["entity"] = _to_dict(ent)
                    get_subgraph = getattr(self.graph, "get_subgraph", None)
                    if callable(get_subgraph):
                        depth = 2
                        subgraph = get_subgraph(entity, depth=depth)
                        results["subgraph"] = {
                            "seed": subgraph.get("seed"),
                            "entities": [_to_dict(e) for e in subgraph.get("entities", [])],
                            "relations": [_to_dict(r) for r in subgraph.get("relations", [])],
                        }
                        steps_taken.append(f"traversed_depth:{depth}")
                get_relations = getattr(self.graph, "get_relations", None)
                if callable(get_relations):
                    relations = get_relations(entity)
                    results["relations"] = [_to_dict(r) for r in relations]
                    steps_taken.append(f"relations:{len(relations)}")

            # Fall back to graph-based retrieval when no direct entity matched.
            if not results:
                retrieve = getattr(self.graph, "retrieve", None)
                if callable(retrieve):
                    chunks = retrieve(query, self.llm, top_k=5)
                    results["retrieved_chunks"] = chunks
                    steps_taken.append("graph_retrieval")

            if not results:
                return SkillResult(
                    success=False,
                    skill_name=self.definition.name,
                    error="No matching entities or relations found in the knowledge graph",
                    steps_taken=steps_taken,
                )

            return SkillResult(
                success=True,
                skill_name=self.definition.name,
                result=results,
                steps_taken=steps_taken,
                metadata={"entity": entity, "used_llm": self.llm is not None},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Knowledge graph query failed")
            return SkillResult(
                success=False,
                skill_name=self.definition.name,
                error=str(exc),
                steps_taken=steps_taken,
            )


# ---------------------------------------------------------------------------
# Advanced skill registry (semantic matching + persistence)
# ---------------------------------------------------------------------------


class _PersistedSkill(Skill):
    """A skill reconstructed from a persisted :class:`SkillDefinition`.

    Used by :meth:`SkillRegistry.load` to restore skill metadata that has no
    bound implementation. Its :meth:`execute` returns a clear error so callers
    know to re-register a concrete skill for the same name.
    """

    def __init__(self, definition: SkillDefinition) -> None:
        self._definition = definition

    @property
    def definition(self) -> SkillDefinition:
        return self._definition

    async def execute(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        return SkillResult(
            success=False,
            skill_name=self._definition.name,
            error=f"Persisted skill '{self._definition.name}' has no bound implementation",
            metadata={"persisted": True},
        )


class SkillRegistry:
    """Registry for managing skills with semantic matching and persistence.

    Enhancements over :class:`doctoragent.model.skills.SkillRegistry`:

    * :meth:`match_skills` returns ranked ``(skill, confidence)`` pairs via
      :class:`SemanticSkillMatcher` instead of a single boolean match.
    * :meth:`execute_best_match` automatically selects and executes the
      highest-confidence skill for a query.
    * :meth:`save` / :meth:`load` persist skill definitions to JSON so custom
      skill catalogs survive across sessions.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._matcher: SemanticSkillMatcher | None = None

    # -- registration ------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """Register a skill (replaces an existing skill with the same name)."""
        self._skills[skill.definition.name] = skill
        self._matcher = None  # invalidate the cached matcher

    def unregister(self, skill_name: str) -> Skill | None:
        """Unregister a skill by name; return the removed skill or ``None``."""
        removed = self._skills.pop(skill_name, None)
        self._matcher = None
        return removed

    def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[SkillDefinition]:
        """List definitions of all registered skills."""
        return [skill.definition for skill in self._skills.values()]

    # -- semantic matching & execution ------------------------------------

    def _get_matcher(self) -> SemanticSkillMatcher:
        """Return the cached matcher, rebuilding it if the registry changed."""
        if self._matcher is None:
            self._matcher = SemanticSkillMatcher(list(self._skills.values()))
        return self._matcher

    def match_skills(
        self,
        query: str,
        top_k: int = 3,
    ) -> list[tuple[Skill, float]]:
        """Return ranked matching skills using :class:`SemanticSkillMatcher`."""
        return self._get_matcher().match(query, top_k=top_k)

    async def execute_best_match(
        self,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Find and execute the best matching skill for *query*."""
        matches = self.match_skills(query, top_k=1)
        if not matches:
            return SkillResult(
                success=False,
                skill_name="none",
                error="No matching skill found for query",
            )
        skill, score = matches[0]
        logger.debug(
            "Executing skill '%s' (confidence=%.3f) for query: %s",
            skill.definition.name,
            score,
            query,
        )
        try:
            result = await skill.execute(query, context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Skill execution failed: %s", skill.definition.name)
            return SkillResult(
                success=False,
                skill_name=skill.definition.name,
                error=str(exc),
                metadata={"confidence": score},
            )
        result.metadata.setdefault("match_confidence", score)
        result.metadata.setdefault("matched_skill", skill.definition.name)
        return result

    # -- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        """Save all registered skill definitions to *path* as JSON."""
        path = Path(path)
        payload = {
            "version": 1,
            "skills": [self._definition_to_dict(definition) for definition in self.list_skills()],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Saved %d skill definitions to %s", len(payload["skills"]), path)

    def load(self, path: Path) -> None:
        """Load skill definitions from *path*, registering stub skills.

        Skills already registered under the same name are left untouched so a
        concrete implementation is never clobbered by a persisted stub.
        """
        path = Path(path)
        if not path.exists():
            logger.warning("Skill file not found: %s", path)
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read skill file %s: %s", path, exc)
            return
        loaded = 0
        for entry in payload.get("skills", []):
            try:
                definition = SkillDefinition(**entry)
            except Exception as exc:  # noqa: BLE001 — skip malformed entries
                logger.warning("Skipping malformed skill definition: %s", exc)
                continue
            if definition.name in self._skills:
                # Keep the already-registered concrete implementation.
                continue
            self._skills[definition.name] = _PersistedSkill(definition)
            loaded += 1
        self._matcher = None
        logger.info("Loaded %d skill definitions from %s", loaded, path)

    @staticmethod
    def _definition_to_dict(definition: SkillDefinition) -> dict[str, Any]:
        """Serialize a :class:`SkillDefinition` to a JSON-safe dict."""
        model_dump = getattr(definition, "model_dump", None)
        if callable(model_dump):
            try:
                return model_dump(mode="json")
            except TypeError:
                return model_dump()
        dict_method = getattr(definition, "dict", None)
        if callable(dict_method):
            return dict_method()
        # Last-resort manual serialization.
        return {
            "name": definition.name,
            "description": definition.description,
            "category": str(definition.category.value)
            if hasattr(definition.category, "value")
            else str(definition.category),
            "triggers": list(definition.triggers),
            "examples": list(definition.examples),
        }


# ---------------------------------------------------------------------------
# Default registry factory
# ---------------------------------------------------------------------------

_ALL_BUILTIN_SKILLS: tuple[type[Skill], ...] = (
    DocumentLifecycleSkill,
    BatchOperationSkill,
    SecurityAuditSkill,
    SyncManagementSkill,
    BackupRestoreSkill,
    ComplianceCheckSkill,
    KnowledgeGraphQuerySkill,
)


def create_default_registry() -> SkillRegistry:
    """Create a registry pre-loaded with all built-in advanced skills.

    Each skill is instantiated without a backing service, so they all operate
    in *simulated* mode. Wire concrete backends (vault manager, scanner,
    knowledge graph, ...) by re-registering constructed instances::

        registry = create_default_registry()
        registry.register(SecurityAuditSkill(security_scanner=my_scanner))
    """
    registry = SkillRegistry()
    for skill_cls in _ALL_BUILTIN_SKILLS:
        registry.register(skill_cls())
    return registry


__all__ = [
    # Enums
    "LifecycleOperation",
    "BatchOperationType",
    "AuditScope",
    "SyncAction",
    "BackupAction",
    "ComplianceFramework",
    # Matching & composition
    "SemanticSkillMatcher",
    "SkillComposer",
    "ComposedSkill",
    # Built-in skills
    "DocumentLifecycleSkill",
    "BatchOperationSkill",
    "SecurityAuditSkill",
    "SyncManagementSkill",
    "BackupRestoreSkill",
    "ComplianceCheckSkill",
    "KnowledgeGraphQuerySkill",
    # Registry
    "SkillRegistry",
    "create_default_registry",
]
