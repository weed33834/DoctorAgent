"""Deterministic drug-drug interaction (DDI) detection engine.

Combines RxNorm (name normalization) with openFDA drug-label text to produce a
conservative, deterministic interaction list. This is the **safety-rule engine
layer** of the clinical stack — LLM agents layer reasoning on top of these
results, but the go/no-go safety decision is always made here, in pure logic.

Design contract:
  * ``check_drug_interactions`` is a pure orchestrator — both the RxNorm and
    openFDA clients are dependency-injected, so the function is trivially
    mockable and never touches the network in unit tests.
  * Severity defaults to ``moderate`` and is only escalated when the label
    text explicitly states ``contraindicated`` / ``major`` / ``minor``.
  * 404s / empty labels from the knowledge sources degrade to "no interaction
    detected" rather than raising.
"""

from __future__ import annotations

import itertools
import logging
import re

from pydantic import BaseModel

from doctoragent.clinical.knowledge.openfda import OpenFDAClient
from doctoragent.clinical.knowledge.rxnorm import RxNormClient

logger = logging.getLogger(__name__)

# Severity ranking — higher = more severe. Used for safety-first sorting.
_SEVERITY_RANK: dict[str, int] = {
    "contraindicated": 4,
    "major": 3,
    "moderate": 2,
    "minor": 1,
    "unknown": 0,
}


# ── 本地结构化 DDI 知识库（不依赖外部 API）──────────────────────────────────
#
# 作为 openFDA 标签文本匹配的补充与优先来源。每条记录包含结构化的
# mechanism / clinical_effect / management，比从标签文本中抽取的描述更可操作。
# ``check_drug_interactions`` 会先查此知识库，命中条目优先返回；未命中的药物对
# 才回退到 openFDA 标签文本匹配。
LOCAL_DDI_KNOWLEDGE: list[dict] = [
    {
        "drug_a": "warfarin",
        "drug_b": "aspirin",
        "severity": "contraindicated",
        "mechanism": "抗血小板与抗凝协同效应，出血风险显著增加",
        "clinical_effect": "消化道出血、颅内出血风险",
        "management": "避免联用；如需联用，加用 PPI 保护胃黏膜，密切监测 INR",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "ibuprofen",
        "severity": "major",
        "mechanism": "NSAID 抗血小板效应 + 胃黏膜损伤 + 华法林代谢竞争",
        "clinical_effect": "出血风险增加",
        "management": "避免联用；如需镇痛，考虑对乙酰氨基酚",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "amiodarone",
        "severity": "major",
        "mechanism": "胺碘酮抑制 CYP2C9，降低华法林清除",
        "clinical_effect": "INR 升高，出血风险",
        "management": "联用时华法林减量 30-50%，频繁监测 INR",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "fluconazole",
        "severity": "major",
        "mechanism": "氟康唑强效抑制 CYP2C9/3A4",
        "clinical_effect": "INR 显著升高",
        "management": "华法林减量 25-50%，监测 INR",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "metronidazole",
        "severity": "major",
        "mechanism": "甲硝唑抑制 CYP2C9",
        "clinical_effect": "INR 升高",
        "management": "华法林减量 25-50%",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "sulfamethoxazole",
        "severity": "major",
        "mechanism": "磺胺类抑制 CYP2C9 + 从蛋白结合位点置换",
        "clinical_effect": "INR 升高",
        "management": "避免联用或华法林减量",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "clarithromycin",
        "severity": "major",
        "mechanism": "克拉霉素抑制 CYP3A4",
        "clinical_effect": "INR 升高",
        "management": "监测 INR，考虑换用阿奇霉素",
    },
    {
        "drug_a": "warfarin",
        "drug_b": "levofloxacin",
        "severity": "moderate",
        "mechanism": "氟喹诺酮类增强抗凝效应",
        "clinical_effect": "INR 可能升高",
        "management": "监测 INR",
    },
    {
        "drug_a": "simvastatin",
        "drug_b": "clarithromycin",
        "severity": "contraindicated",
        "mechanism": "克拉霉素强效抑制 CYP3A4",
        "clinical_effect": "横纹肌溶解风险",
        "management": "禁用；暂停他汀或换用普伐他汀",
    },
    {
        "drug_a": "simvastatin",
        "drug_b": "amiodarone",
        "severity": "major",
        "mechanism": "胺碘酮抑制 CYP3A4",
        "clinical_effect": "横纹肌溶解风险",
        "management": "辛伐他汀剂量不超过 20mg/日",
    },
    {
        "drug_a": "simvastatin",
        "drug_b": "diltiazem",
        "severity": "major",
        "mechanism": "地尔硫卓抑制 CYP3A4",
        "clinical_effect": "肌病风险",
        "management": "辛伐他汀剂量不超过 10mg/日",
    },
    {
        "drug_a": "lisinopril",
        "drug_b": "spironolactone",
        "severity": "major",
        "mechanism": "ACEI + 保钾利尿剂",
        "clinical_effect": "高钾血症",
        "management": "密切监测血钾",
    },
    {
        "drug_a": "enalapril",
        "drug_b": "potassium",
        "severity": "major",
        "mechanism": "ACEI 减少醛固酮，减少钾排泄",
        "clinical_effect": "高钾血症",
        "management": "监测血钾",
    },
    {
        "drug_a": "metformin",
        "drug_b": "contrast",
        "severity": "major",
        "mechanism": "造影剂 + 二甲双胍 = 乳酸酸中毒风险",
        "clinical_effect": "乳酸酸中毒",
        "management": "造影前 48h 停用二甲双胍，造影后确认肾功能正常后恢复",
    },
    {
        "drug_a": "methotrexate",
        "drug_b": "trimethoprim-sulfamethoxazole",
        "severity": "contraindicated",
        "mechanism": "TMP 抑制二氢叶酸还原酶，与 MTX 协同",
        "clinical_effect": "严重骨髓抑制",
        "management": "禁用",
    },
    {
        "drug_a": "clonidine",
        "drug_b": "propranolol",
        "severity": "major",
        "mechanism": "突然停药反跳性高血压风险",
        "clinical_effect": "高血压危象",
        "management": "先停 β 阻滞剂，再逐步停可乐定",
    },
    {
        "drug_a": "sildenafil",
        "drug_b": "nitroglycerin",
        "severity": "contraindicated",
        "mechanism": "协同血管扩张",
        "clinical_effect": "严重低血压",
        "management": "禁用，间隔至少 24h（他达拉非 48h）",
    },
    {
        "drug_a": "tramadol",
        "drug_b": "sertraline",
        "severity": "major",
        "mechanism": "5-HT 综合征风险",
        "clinical_effect": "5-HT 综合征、癫痫发作",
        "management": "避免联用",
    },
    {
        "drug_a": "codeine",
        "drug_b": "fluconazole",
        "severity": "moderate",
        "mechanism": "氟康唑抑制 CYP2D6，减少吗啡转化",
        "clinical_effect": "镇痛效果降低",
        "management": "考虑替代镇痛方案",
    },
]


def check_local_ddi(medications: list[str]) -> list[dict]:
    """查询本地结构化 DDI 知识库，返回药物相互作用列表。

    匹配方式为药名包含关系（不区分大小写）：知识库条目的 ``drug_a`` / ``drug_b``
    作为子串在患者用药列表中查找，如 ``"warfarin"`` 匹配 ``"Warfarin 5mg"``。

    Args:
        medications: 患者用药名称列表（可为 ``"Warfarin 5mg"`` 这样带剂量的文本）。

    Returns:
        字典列表，每个字典包含 ``drug_a`` / ``drug_b``（实际匹配的患者用药名）、
        ``severity``、``mechanism``、``clinical_effect``、``management``、
        ``source="local"``。同一对患者用药仅返回一次。
    """
    if not medications or len(medications) < 2:
        return []
    meds_indexed = [
        (idx, med, med.lower())
        for idx, med in enumerate(medications)
        if isinstance(med, str) and med.strip()
    ]
    results: list[dict] = []
    seen: set[frozenset[str]] = set()
    for entry in LOCAL_DDI_KNOWLEDGE:
        a_lower = entry["drug_a"].lower()
        b_lower = entry["drug_b"].lower()
        a_matches = [(i, med) for i, med, ml in meds_indexed if a_lower in ml]
        b_matches = [(j, med) for j, med, ml in meds_indexed if b_lower in ml]
        for i, med_a in a_matches:
            for j, med_b in b_matches:
                # 跳过同一药品（按索引）与同名药品自配对
                if i == j or med_a.lower() == med_b.lower():
                    continue
                key = frozenset((med_a.lower(), med_b.lower()))
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "drug_a": med_a,
                        "drug_b": med_b,
                        "severity": entry["severity"],
                        "mechanism": entry["mechanism"],
                        "clinical_effect": entry["clinical_effect"],
                        "management": entry["management"],
                        "source": "local",
                    }
                )
    return results


class DrugInteractionResult(BaseModel):
    """A single detected drug-drug interaction.

    Allowed ``severity`` values: ``contraindicated | major | moderate | minor |
    unknown``. Allowed ``source`` values: ``local | openfda | rxnorm``.
    """

    drug_a: str
    drug_b: str
    severity: str = "moderate"
    description: str = ""
    mechanism: str = ""
    clinical_effect: str = ""
    management: str = ""
    source: str = "openfda"


def get_severity_rank(severity: str) -> int:
    """Return numeric rank for a severity label (higher = more severe)."""
    return _SEVERITY_RANK.get(severity.lower(), 0)


def _detect_mention(text: str, drug_name: str) -> bool:
    """Return True if *drug_name* appears as a whole word in *text*."""
    if not text or not drug_name:
        return False
    pattern = r"\b" + re.escape(drug_name.lower()) + r"\b"
    return re.search(pattern, text.lower()) is not None


def _detect_severity(text: str) -> str:
    """Heuristic severity classification from label text.

    Defaults to ``moderate`` per spec; escalates only when the label explicitly
    states a stronger keyword.
    """
    if not text:
        return "moderate"
    lowered = text.lower()
    if "contraindicated" in lowered or "contraindication" in lowered:
        return "contraindicated"
    if "major" in lowered:
        return "major"
    if "minor" in lowered:
        return "minor"
    return "moderate"


def _extract_context(text: str, drug_name: str, window: int = 200) -> str:
    """Return a text window around the first mention of *drug_name*."""
    if not text or not drug_name:
        return ""
    idx = text.lower().find(drug_name.lower())
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(drug_name) + window)
    return text[start:end].strip().replace("\n", " ")


async def check_drug_interactions(
    drugs: list[str],
    rxnorm: RxNormClient | None = None,
    openfda: OpenFDAClient | None = None,
) -> list[DrugInteractionResult]:
    """Check all C(n, 2) drug pairs for interactions.

    先查 :data:`LOCAL_DDI_KNOWLEDGE` 本地结构化知识库（不依赖外部 API），命中
    条目优先返回（带结构化的 mechanism/clinical_effect/management）；未命中的
    药物对再回退到 openFDA 标签文本匹配。本地结果与 openFDA 结果合并后按严重度
    降序排列。

    Args:
        drugs: List of drug names (generic or brand) to cross-check.
        rxnorm: Optional pre-built :class:`RxNormClient`. A throwaway instance
            is created (and closed) when ``None``. 仅在存在未覆盖药物对时使用。
        openfda: Optional pre-built :class:`OpenFDAClient`. A throwaway instance
            is created (and closed) when ``None``. 仅在存在未覆盖药物对时使用。

    Returns:
        All detected interactions, sorted by severity (most severe first).
    """
    if len(drugs) < 2:
        return []

    # 1) 先查本地结构化 DDI 知识库（优先来源，不依赖外部 API）
    local_hits = check_local_ddi(drugs)
    results: list[DrugInteractionResult] = []
    covered_pairs: set[frozenset[str]] = set()
    for hit in local_hits:
        results.append(
            DrugInteractionResult(
                drug_a=hit["drug_a"],
                drug_b=hit["drug_b"],
                severity=hit["severity"],
                description=hit["clinical_effect"] or hit["mechanism"],
                mechanism=hit["mechanism"],
                clinical_effect=hit["clinical_effect"],
                management=hit["management"],
                source="local",
            )
        )
        covered_pairs.add(frozenset((hit["drug_a"].lower(), hit["drug_b"].lower())))

    # 2) 找出未被本地知识库覆盖的药物对，回退到 openFDA 标签文本匹配
    uncovered = [
        (drug_a, drug_b)
        for drug_a, drug_b in itertools.combinations(drugs, 2)
        if frozenset((drug_a.lower(), drug_b.lower())) not in covered_pairs
    ]

    if uncovered:
        owns_rxnorm = rxnorm is None
        owns_openfda = openfda is None
        if rxnorm is None:
            rxnorm = RxNormClient()
        if openfda is None:
            openfda = OpenFDAClient()
        try:
            # Pre-normalize names + fetch interaction labels in one pass per drug.
            normalized: dict[str, str | None] = {}
            interaction_texts: dict[str, str] = {}
            for drug in drugs:
                try:
                    normalized[drug] = await rxnorm.normalize_drug_name(drug)
                except Exception:
                    logger.warning("RxNorm normalization failed for %r", drug, exc_info=True)
                    normalized[drug] = None
                try:
                    interaction_texts[drug] = await openfda.get_interactions_section(drug)
                except Exception:
                    logger.warning(
                        "openFDA interactions lookup failed for %r",
                        drug,
                        exc_info=True,
                    )
                    interaction_texts[drug] = ""

            for drug_a, drug_b in uncovered:
                text_a = interaction_texts.get(drug_a, "")
                text_b = interaction_texts.get(drug_b, "")
                mentions_b_in_a = _detect_mention(text_a, drug_b)
                mentions_a_in_b = _detect_mention(text_b, drug_a)
                if not (mentions_b_in_a or mentions_a_in_b):
                    continue

                # Use whichever label explicitly mentions the other drug.
                if mentions_b_in_a:
                    primary_text, context_drug = text_a, drug_b
                else:
                    primary_text, context_drug = text_b, drug_a

                context = _extract_context(primary_text, context_drug)
                severity = _detect_severity(context or primary_text)

                results.append(
                    DrugInteractionResult(
                        drug_a=drug_a,
                        drug_b=drug_b,
                        severity=severity,
                        description=context or primary_text[:300],
                        mechanism="",
                        clinical_effect="",
                        management="",
                        source="openfda",
                    )
                )
        finally:
            if owns_rxnorm:
                await rxnorm.close()
            if owns_openfda:
                await openfda.close()

    # Safety-first presentation: most severe interactions on top.
    results.sort(key=lambda r: get_severity_rank(r.severity), reverse=True)
    return results
