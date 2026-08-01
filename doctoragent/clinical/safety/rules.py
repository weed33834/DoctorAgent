"""Deterministic clinical rule engine.

Safety-first rule layer that sits *below* the LLM agent. It wraps the
reference-range abnormality detector (:mod:`reference_ranges`), the
drug-drug interaction engine (:mod:`drug_interactions`) and a small set of
locally-decidable checks (allergy cross-reactivity, duplicate therapy) so
that go/no-go safety decisions are made in pure, auditable logic rather
than in model output.

Design contract
---------------
* Pure logic + dependency injection. The RxNorm / openFDA clients are
  optional — when absent the engine still runs every rule that does not
  need external knowledge data (allergy + duplicate therapy + vitals +
  labs). No network access happens inside this module.
* ``evaluate_medications`` is the only ``async`` method because it may
  await the injected knowledge clients; everything else is synchronous.
* Results are :class:`ClinicalRuleResult` (pydantic) so they serialise
  cleanly into FHIR / compliance reports.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from doctoragent.clinical.fhir.parser import coding_display
from doctoragent.clinical.knowledge.drug_interactions import (
    DrugInteractionResult,
    check_drug_interactions,
)
from doctoragent.clinical.knowledge.openfda import OpenFDAClient
from doctoragent.clinical.knowledge.rxnorm import RxNormClient
from doctoragent.clinical.safety.reference_ranges import (
    AbnormalityFlag,
    evaluate_lab_value,
    evaluate_vitals,
    get_reference_range,
)

__all__ = [
    "ALLERGY_CROSS_REACTIVITY",
    "CROSS_REACTIVITY_SEVERITY",
    "ClinicalRuleEngine",
    "ClinicalRuleResult",
    "ClinicalRuleType",
    "get_allergy_cross_reactivity",
    "get_allergy_cross_reactivity_warnings",
    "get_clinical_rules_version",
]


# 临床规则知识库版本
CLINICAL_RULES_VERSION = "1.1.0"
CLINICAL_RULES_CHANGELOG = "扩展过敏交叉反应至15+类，增强 DDI 检测"


def get_clinical_rules_version() -> str:
    """获取临床规则知识库版本号。"""
    return CLINICAL_RULES_VERSION


class ClinicalRuleType(str, Enum):
    """Categories of deterministic clinical rules the engine can fire."""

    VITALS = "vitals"
    LABS = "labs"
    DRUG_INTERACTION = "drug_interaction"
    ALLERGY = "allergy"
    DUPLICATE_THERAPY = "duplicate_therapy"
    DOSAGE = "dosage"


class ClinicalRuleResult(BaseModel):
    """A single rule firing produced by :class:`ClinicalRuleEngine`.

    ``severity`` is one of ``info | warning | critical | contraindicated``;
    ``critical`` and ``contraindicated`` findings are *blocking* and must be
    acknowledged by a clinician before downstream automation proceeds.
    """

    rule_type: ClinicalRuleType
    severity: str
    finding: str
    affected_resources: list[str] = Field(default_factory=list)
    recommendation: str
    source: str = "rule_engine"


# ── Allergy cross-reactivity map ────────────────────────────────────────────
#
# Each entry maps a canonical allergy *class* to the aliases a patient might
# use to describe it and the drug-name substrings that indicate membership of
# that class. Matching is intentionally conservative (substring + class map)
# — false negatives are acceptable, false positives force human review which
# is the safe default for an allergy check.
_ALLERGY_CLASSES: list[dict[str, list[str]]] = [
    {
        "names": ["penicillin", "penicillins", "beta-lactam", "beta lactam"],
        "drug_substrings": [
            "penicillin",
            "cillin",
            "amoxicillin",
            "ampicillin",
            "amox",
            "augmentin",
            "piperacillin",
            "flucloxacillin",
            "dicloxacillin",
            "nafcillin",
            "oxacillin",
            "benzylpenicillin",
        ],
    },
    {
        "names": ["sulfa", "sulpha", "sulfonamide", "sulfonamides"],
        "drug_substrings": [
            "sulfa",
            "sulfamethoxazole",
            "sulfasalazine",
            "sulfacetamide",
            "co-trimoxazole",
            "cotrimoxazole",
            "bactrim",
            "septra",
            "sulfisoxazole",
        ],
    },
    {
        "names": ["aspirin", "asa", "nsaid", "nsaids"],
        "drug_substrings": [
            "aspirin",
            "ibuprofen",
            "naproxen",
            "diclofenac",
            "ketorolac",
            "indomethacin",
            "celecoxib",
            "meloxicam",
            "etodolac",
            "ketoprofen",
        ],
    },
]


# ── 扩展的过敏交叉反应映射（3 类 → 15+ 类）─────────────────────────────────
#
# 每个 key 是一个规范的过敏反应*类*，value 是与该类存在交叉反应风险的药物子串
# 列表。匹配策略为大小写不敏感的子串包含（如 "amoxicillin" 匹配 "Amoxicillin 500mg"）。
# 当 value 中某项本身也是本映射的 key（例如 "cephalosporin" 出现在 "penicillin" 的
# 列表中），匹配会递归一层——即只要患者用药落入该子类（如 ceftriaxone 属于
# cephalosporin 类），即视为与原过敏原存在交叉反应。这让头孢-青霉素等跨类交叉
# 反应对具体药物也能命中。
ALLERGY_CROSS_REACTIVITY: dict[str, list[str]] = {
    # === 已有的 3 类（保留） ===
    "penicillin": [
        "amoxicillin",
        "ampicillin",
        "cloxacillin",
        "flucloxacillin",
        "piperacillin",
        "ticarcillin",
        "cephalosporin",  # 头孢与青霉素交叉反应率 1-10%
    ],
    "sulfa": [
        "sulfamethoxazole",
        "sulfasalazine",
        "trimethoprim-sulfamethoxazole",
        "furosemide",
        "hydrochlorothiazide",
    ],
    "aspirin": [
        "ibuprofen",
        "naproxen",
        "diclofenac",
        "ketorolac",
        "indomethacin",
        "mefenamic acid",
        "celecoxib",
        "etoricoxib",
    ],
    # === 新增 12+ 类 ===
    "cephalosporin": [
        "cephalexin",
        "cefuroxime",
        "ceftriaxone",
        "ceftazidime",
        "cefepime",
        "ceftaroline",
    ],
    "iodine": [
        "iodinated contrast",
        "povidone-iodine",
        "amiodarone",
        "kelp",
        "放射性造影剂",
    ],
    "latex": ["rubber", "balloon", "glove", "catheter"],
    "statin": [
        "simvastatin",
        "atorvastatin",
        "rosuvastatin",
        "pravastatin",
        "fluvastatin",
        "pitavastatin",
    ],
    "antiepileptic": [
        "carbamazepine",
        "oxcarbazepine",
        "phenytoin",
        "lamotrigine",
        "levetiracetam",
        "valproate",
    ],
    "opioid": [
        "morphine",
        "codeine",
        "tramadol",
        "fentanyl",
        "oxycodone",
        "hydrocodone",
        "hydromorphone",
    ],
    "chemotherapy": [
        "paclitaxel",
        "docetaxel",
        "cisplatin",
        "carboplatin",
        "oxaliplatin",
    ],
    "nsaid_full": [
        "ibuprofen",
        "naproxen",
        "diclofenac",
        "ketorolac",
        "indomethacin",
        "mefenamic acid",
        "celecoxib",
        "etoricoxib",
        "aspirin",
        "sulindac",
        "piroxicam",
        "meloxicam",
        "nabumetone",
        "etodolac",
        "ketoprofen",
        "flurbiprofen",
        "tolmetin",
        "diflunisal",
        "nimesulide",
    ],
    "corticosteroid": [
        "prednisone",
        "prednisolone",
        "dexamethasone",
        "methylprednisolone",
        "hydrocortisone",
        "betamethasone",
    ],
    "local_anesthetic": [
        "lidocaine",
        "bupivacaine",
        "mepivacaine",
        "ropivacaine",
        "articaine",
    ],
    "muscle_relaxant": [
        "succinylcholine",
        "rocuronium",
        "vecuronium",
        "atracurium",
        "cisatracurium",
        "mivacurium",
    ],
    "proton_pump_inhibitor": [
        "omeprazole",
        "esomeprazole",
        "lansoprazole",
        "pantoprazole",
        "rabeprazole",
        "dexlansoprazole",
    ],
    "insulin": [
        "insulin lispro",
        "insulin aspart",
        "insulin glargine",
        "insulin detemir",
        "insulin degludec",
        "regular insulin",
        "nph insulin",
    ],
    "heparin": [
        "unfractionated heparin",
        "low molecular weight heparin",
        "enoxaparin",
        "dalteparin",
        "fondaparinux",
    ],
}


# 交叉反应严重度分级。键为 (过敏原类, 交叉反应药物/类) 元组。
# 未列出的组合默认 ``contraindicated``（同类直接过敏反应最严重）；
# 跨类反应（如青霉素-头孢）降为 ``warning``，可致命反应（造影剂、乳胶、
# 神经肌肉阻滞剂、铂类再激发）升为 ``critical``。
CROSS_REACTIVITY_SEVERITY: dict[tuple[str, str], str] = {
    ("penicillin", "cephalosporin"): "warning",  # 1-10% 交叉反应
    ("iodine", "iodinated contrast"): "critical",  # 造影剂过敏可致命
    ("latex", "latex"): "critical",
    ("muscle_relaxant", "succinylcholine"): "critical",  # 过敏反应最严重
    ("chemotherapy", "cisplatin"): "critical",  # 铂类再激发
}


def _allergy_matches_class(allergy_lower: str, class_key: str, cross_drugs: list[str]) -> bool:
    """判断过敏原是否属于给定交叉反应类。

    匹配规则（任一命中即视为同类）：
      1. 类名与过敏原互为子串（处理 ``"penicillin allergy"`` 等 FHIR 文本）；
      2. 过敏原本身是该类内某种药物（如 ``"morphine"`` 属于 ``"opioid"`` 类）。
    """
    if class_key in allergy_lower or allergy_lower in class_key:
        return True
    for drug in cross_drugs:
        drug_lower = drug.lower()
        if drug_lower == allergy_lower:
            return True
        # 避免短串误匹配：仅当 drug 长度 >= 4 时才做子串判断
        if len(drug_lower) >= 4 and (drug_lower in allergy_lower or allergy_lower in drug_lower):
            return True
    return False


def get_allergy_cross_reactivity_warnings(drug_name: str, allergies: list[str]) -> list[dict]:
    """返回结构化的过敏交叉反应警告列表。

    遍历 :data:`ALLERGY_CROSS_REACTIVITY` 扩展映射以及原有的
    :data:`_ALLERGY_CLASSES` 别名表，检测 *drug_name* 与患者过敏列表
    *allergies* 之间的交叉反应风险。

    每个警告为一个字典，包含：
      * ``allergen`` —— 触发警告的患者过敏原（原样保留大小写）；
      * ``cross_reactive_drug`` —— 命中的交叉反应药物/类标识；
      * ``severity`` —— ``contraindicated | critical | warning`` 之一，
        由 :data:`CROSS_REACTIVITY_SEVERITY` 决定，未列出则默认
        ``contraindicated``（同类直接过敏反应）。

    同一 (过敏原, 药物) 组合仅返回一次（取命中的最高严重度）；
    返回顺序遵循 *allergies*。
    """
    if not drug_name or not allergies:
        return []
    drug_lower = drug_name.lower()

    # 严重度排序：contraindicated > critical > warning > info
    _sev_rank = {"contraindicated": 4, "critical": 3, "warning": 2, "info": 1}

    # 候选警告按 (allergy_lower, drug_lower) 聚合，保留最高严重度
    best: dict[tuple[str, str], dict] = {}

    def _add(allergen: str, cross_drug: str, severity: str) -> None:
        key = (allergen.lower(), drug_lower)
        new_rank = _sev_rank.get(severity, 0)
        existing = best.get(key)
        if existing is None or new_rank > _sev_rank.get(existing["severity"], 0):
            best[key] = {
                "allergen": allergen,
                "cross_reactive_drug": cross_drug,
                "severity": severity,
            }

    for allergy in allergies:
        if not allergy:
            continue
        allergy_lower = allergy.lower().strip()
        if not allergy_lower:
            continue

        # 1) 直接子串匹配（过敏原名出现在药名中，如 "aspirin" vs "Aspirin 325mg"）
        if allergy_lower in drug_lower:
            _add(allergy, drug_name, "contraindicated")

        # 2) 通过 _ALLERGY_CLASSES 别名表匹配（保留 beta-lactam/asa/bactrim 等别名能力）
        for cls in _ALLERGY_CLASSES:
            allergy_in_class = any(name in allergy_lower for name in cls["names"])
            drug_in_class = any(sub in drug_lower for sub in cls["drug_substrings"])
            if allergy_in_class and drug_in_class:
                _add(allergy, drug_name, "contraindicated")
                break

        # 3) 通过扩展映射 ALLERGY_CROSS_REACTIVITY 匹配（支持 15+ 类与跨类递归）
        for class_key, cross_drugs in ALLERGY_CROSS_REACTIVITY.items():
            if not _allergy_matches_class(allergy_lower, class_key, cross_drugs):
                continue
            for cross_drug in cross_drugs:
                cd_lower = cross_drug.lower()
                # 3a) 直接命中：交叉药物是药名的子串
                if cd_lower in drug_lower:
                    severity = CROSS_REACTIVITY_SEVERITY.get(
                        (class_key, cross_drug), "contraindicated"
                    )
                    _add(allergy, cross_drug, severity)
                # 3b) 间接命中：交叉药物本身是另一个类（如 "cephalosporin"），
                #      检查患者用药是否落入该子类（如 ceftriaxone）
                elif cross_drug in ALLERGY_CROSS_REACTIVITY and cross_drug != class_key:
                    for sub_drug in ALLERGY_CROSS_REACTIVITY[cross_drug]:
                        if sub_drug.lower() in drug_lower:
                            severity = CROSS_REACTIVITY_SEVERITY.get(
                                (class_key, cross_drug), "contraindicated"
                            )
                            _add(allergy, cross_drug, severity)
                            break

    # 按 allergies 顺序输出（保持稳定、可预测的排序）
    order = {a.lower().strip(): i for i, a in enumerate(allergies) if a}
    warnings = list(best.values())
    warnings.sort(key=lambda w: order.get(w["allergen"].lower(), len(order)))
    return warnings


def get_allergy_cross_reactivity(drug_name: str, allergies: list[str]) -> list[str]:
    """Return the entries from *allergies* that match *drug_name*.

    A match is either a direct substring relation (allergy name appears in
    the drug name) or a class-based cross-reaction (allergy + drug both map
    to the same canonical class, e.g. ``penicillin`` allergy vs.
    ``amoxicillin`` drug). Detection now also consults the expanded
    :data:`ALLERGY_CROSS_REACTIVITY` map (15+ classes, including跨类递归匹配
    如青霉素-头孢)。Order of the returned list follows *allergies*;
    duplicates are collapsed.

    返回匹配的过敏原名称列表（保留原签名与返回类型以维持向后兼容）；
    如需严重度分级的结构化警告，请使用 :func:`get_allergy_cross_reactivity_warnings`。
    """
    hits: list[str] = []
    seen: set[str] = set()
    for warning in get_allergy_cross_reactivity_warnings(drug_name, allergies):
        allergen = warning["allergen"]
        if allergen not in seen:
            hits.append(allergen)
            seen.add(allergen)
    return hits


# ── Duplicate therapy detection ─────────────────────────────────────────────
#
# Strip dosage tokens (``500mg``, ``10 mL``, ``q8h`` …) and compare the
# leading token of what remains. Two drugs sharing the same leading token
# are treated as potential duplicate therapy. This is deliberately simple —
# it catches ``Acetaminophen 500mg`` vs ``Acetaminophen 325mg`` but will not
# resolve brand↔generic equivalence (``Tylenol`` vs ``Acetaminophen``), which
# would require RxNorm.
_DOSAGE_TOKEN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|ml|cc|iu|units?|meq|mmol|%|m?eq)?\b",
    re.IGNORECASE,
)
_ROUTE_TOKENS = re.compile(
    r"\b(?:po|iv|im|sc|pr|sl|od|bd|tds|qid|q\d+h|prn|stat)\b",
    re.IGNORECASE,
)


def _normalize_drug_base(name: str) -> str:
    """Return the leading active-ingredient token of *name* (lowercased)."""
    cleaned = name.lower().strip()
    cleaned = _DOSAGE_TOKEN.sub(" ", cleaned)
    cleaned = _ROUTE_TOKENS.sub(" ", cleaned)
    cleaned = re.sub(r"[^a-z\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.split()[0] if cleaned else ""


def _normalize_medication(med: Any) -> str:
    """Extract a drug-name string from a medication entry.

    Accepts both the ``list[str]`` contract the engine documents *and* the
    dict shapes real-world callers pass (FHIR ``MedicationRequest`` excerpts,
    simplified ``{"name": ...}`` / ``{"drug": ...}`` patient-context entries).
    Returns an empty string for unusable input so downstream rules skip it
    rather than raising ``AttributeError`` on ``.lower()``.
    """
    if isinstance(med, str):
        return med.strip()
    if isinstance(med, dict):
        for key in ("name", "drug", "substance", "display", "text"):
            val = med.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # FHIR MedicationRequest: medicationCodeableConcept.text /
        # .coding[0].display — reuse the canonical CodeableConcept display
        # extractor so the text → coding[0].display → top-display walk is
        # not duplicated here.
        display = coding_display(med.get("medicationCodeableConcept"))
        if display:
            return display
    return ""


def _normalize_allergy(allergy: Any) -> str:
    """Extract an allergen name string from an allergy entry.

    Mirrors :func:`_normalize_medication` for the allergy side — handles
    bare strings and dict shapes (``{"substance": ...}``,
    FHIR ``AllergyIntolerance.code`` excerpts).
    """
    if isinstance(allergy, str):
        return allergy.strip()
    if isinstance(allergy, dict):
        for key in ("substance", "name", "display", "text"):
            val = allergy.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # FHIR AllergyIntolerance.code — reuse the canonical CodeableConcept
        # display extractor (text → coding[0].display → top-display).
        display = coding_display(allergy.get("code"))
        if display:
            return display
    return ""


def _normalize_labs(labs: Any) -> list[dict]:
    """Coerce varied lab-input shapes into the canonical ``list[dict]`` form.

    The rule engine's documented contract is
    ``[{test, value, unit?, gender?, id?}, ...]``, but real callers pass:

    * ``list[dict]`` — the canonical form (passed through unchanged);
    * ``list[str|number]`` — positional ``[value, value, ...]`` is *not*
      supported (no test names) and is skipped;
    * ``dict[str, float|int|str]`` — shorthand ``{test: value}`` mirroring
      ``vitals`` (the API console placeholder uses this shape);
    * ``dict[str, dict]`` — ``{test: {value, unit?, gender?, id?}}``,
      a common FHIR-extracted layout.

    Invalid entries (missing test/value, unparseable value) are dropped so
    downstream rules never raise; ``test`` and ``value`` are always strings
    / numbers respectively in the returned dicts.
    """
    if isinstance(labs, dict):
        out: list[dict] = []
        for test, payload in labs.items():
            if isinstance(payload, dict):
                entry = dict(payload)
                entry.setdefault("test", test)
                out.append(entry)
            elif payload is not None and not isinstance(payload, (list, tuple)):
                out.append({"test": test, "value": payload})
        return out
    if isinstance(labs, list):
        return [lab for lab in labs if isinstance(lab, dict)]
    return []


def _detect_duplicate_therapy(
    medications: list[str],
) -> list[tuple[str, str]]:
    """Return ``[(med_a, med_b)]`` pairs that share a normalised base name."""
    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    for med in medications:
        base = _normalize_drug_base(med)
        if not base:
            continue
        if base in seen:
            duplicates.append((seen[base], med))
        else:
            seen[base] = med
    return duplicates


async def _detect_duplicate_therapy_rxnorm(
    medications: list[str],
    rxnorm_client: RxNormClient,
) -> list[tuple[str, str, str]]:
    """RxNorm-aware duplicate therapy detection.

    Resolves each medication to its ingredient-level RxCUI via
    :meth:`RxNormClient.normalize_drug_name` + ``get_related_drugs`` (TTY=IN)
    and groups drugs that share the same active ingredient. This catches
    brand↔generic equivalents (``Tylenol`` vs ``Acetaminophen``) and
    multi-brand duplicates (``Vicodin`` vs ``Norco``) that the local
    name-based detector misses.

    Returns ``[(med_a, med_b, ingredient)]`` triples. Failures to resolve
    a drug (network error, RxCUI not found) are silently skipped — the
    safety floor (the local detector) still runs.
    """
    # Step 1: resolve each med to its ingredient RxCUI.
    # ingredient_rxcui -> first med seen with that ingredient
    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str, str]] = []
    for med in medications:
        try:
            rxcui = await rxnorm_client.normalize_drug_name(med)
        except Exception:  # noqa: BLE001 — network/parse failures skip this med
            continue
        if not rxcui:
            continue
        try:
            related = await rxnorm_client.get_related_drugs(rxcui, relation="IN")
        except Exception:  # noqa: BLE001
            continue
        # The ingredient is the first IN (ingredient) concept, if any.
        # Fall back to the rxcui itself when no IN is returned (rare).
        ingredient = related[0]["name"] if related else rxcui
        if ingredient in seen:
            duplicates.append((seen[ingredient], med, ingredient))
        else:
            seen[ingredient] = med
    return duplicates


# DDI severity → engine severity. ``contraindicated`` is preserved verbatim
# so it lands in the blocking set; ``major`` is surfaced as ``critical``.
_DDI_SEVERITY_MAP: dict[str, str] = {
    "contraindicated": "contraindicated",
    "major": "critical",
    "moderate": "warning",
    "minor": "info",
    "unknown": "info",
}


class ClinicalRuleEngine:
    """Deterministic clinical rule engine.

    Parameters
    ----------
    rxnorm_client, openfda_client:
        Optional pre-built knowledge clients. When *both* are supplied the
        engine will run drug-drug interaction checks; when either is
        ``None`` DDI is skipped and only locally-decidable rules (allergy,
        duplicate therapy, vitals, labs) run. The engine never closes
        injected clients — ownership stays with the caller.
    """

    def __init__(
        self,
        rxnorm_client: RxNormClient | None = None,
        openfda_client: OpenFDAClient | None = None,
    ) -> None:
        self.rxnorm_client = rxnorm_client
        self.openfda_client = openfda_client

    # ── vitals ─────────────────────────────────────────────────────────

    def evaluate_vitals(self, vitals: dict[str, float]) -> list[ClinicalRuleResult]:
        """Evaluate a ``{test_name: value}`` mapping of vital signs."""
        results: list[ClinicalRuleResult] = []
        for ev in evaluate_vitals(vitals):
            flag = ev["flag"]
            if flag in (AbnormalityFlag.NORMAL, AbnormalityFlag.UNKNOWN):
                continue
            test = ev["test"]
            value = ev["value"]
            unit = ev.get("unit") or ""
            ref_str = ev.get("reference") or ""
            ref = get_reference_range(test) or {}
            severity, finding, recommendation = self._abnormality_finding(
                flag, test, value, unit, ref_str, ref
            )
            results.append(
                ClinicalRuleResult(
                    rule_type=ClinicalRuleType.VITALS,
                    severity=severity,
                    finding=finding,
                    affected_resources=[test],
                    recommendation=recommendation,
                )
            )
        return results

    @staticmethod
    def _abnormality_finding(
        flag: AbnormalityFlag,
        test: str,
        value: float,
        unit: str | None,
        ref_str: str,
        ref: dict,
    ) -> tuple[str, str, str]:
        unit_part = f" {unit}" if unit else ""
        if flag is AbnormalityFlag.CRITICAL_LOW:
            crit = ref.get("critical_low")
            return (
                "critical",
                f"{test} {value}{unit_part} 低于危急值 {crit}",
                f"立即复核 {test}，评估紧急临床干预",
            )
        if flag is AbnormalityFlag.CRITICAL_HIGH:
            crit = ref.get("critical_high")
            return (
                "critical",
                f"{test} {value}{unit_part} 高于危急值 {crit}",
                f"立即复核 {test}，评估紧急临床干预",
            )
        if flag is AbnormalityFlag.LOW:
            return (
                "warning",
                f"{test} {value}{unit_part} 低于参考范围 {ref_str}",
                "结合临床评估，必要时复查",
            )
        # HIGH
        return (
            "warning",
            f"{test} {value}{unit_part} 高于参考范围 {ref_str}",
            "结合临床评估，必要时复查",
        )

    # ── labs ───────────────────────────────────────────────────────────

    def evaluate_labs(self, labs: list[dict] | dict[str, Any]) -> list[ClinicalRuleResult]:
        """Evaluate lab results.

        Accepts three common shapes (the documented contract plus two
        caller-friendly shorthands) so the engine never raises on the
        variety of inputs real-world patient_contexts carry:

        * ``list[dict]`` — canonical ``{test, value, unit?, gender?, id?}``.
        * ``dict[str, float]`` — shorthand ``{test: value}`` mirroring
          ``vitals`` (the shape the API console placeholder and many test
          fixtures use).
        * ``dict[str, dict]`` — ``{test: {value, unit?, gender?, id?}}``,
          a common FHIR-extracted layout.
        """
        results: list[ClinicalRuleResult] = []
        for lab in _normalize_labs(labs):
            test = lab.get("test")
            raw_value = lab.get("value")
            if test is None or raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            unit = lab.get("unit")
            gender = lab.get("gender", "male")
            resource_id = lab.get("id") or str(test)
            ev = evaluate_lab_value(str(test), value, unit=unit, gender=gender)
            flag = ev["flag"]
            if flag in (AbnormalityFlag.NORMAL, AbnormalityFlag.UNKNOWN):
                continue
            ref = ev.get("reference_range") or {}
            ref_str = f"{ref.get('min')}-{ref.get('max')}" if ref else ""
            severity, finding, recommendation = ClinicalRuleEngine._abnormality_finding(
                flag, str(test), value, unit, ref_str, ref
            )
            results.append(
                ClinicalRuleResult(
                    rule_type=ClinicalRuleType.LABS,
                    severity=severity,
                    finding=finding,
                    affected_resources=[resource_id],
                    recommendation=recommendation,
                )
            )
        return results

    # ── medications ────────────────────────────────────────────────────

    async def evaluate_medications(
        self,
        medications: list[Any],
        allergies: list[Any] | None = None,
    ) -> list[ClinicalRuleResult]:
        """Run DDI, allergy cross-reactivity and duplicate-therapy checks.

        Accepts medications/allergies as either bare strings or dict shapes
        (FHIR ``MedicationRequest`` / ``AllergyIntolerance`` excerpts, or
        simplified ``{"name": ...}`` patient-context entries). Each entry is
        normalised to a drug/allergen name string before any downstream
        rule sees it, so the pure-logic helpers never raise on a dict.
        """
        # Normalise to strings once, at the entry point, so every downstream
        # consumer (DDI engine, cross-reactivity, duplicate-therapy) receives
        # the documented ``list[str]`` contract regardless of caller shape.
        med_names = [_normalize_medication(m) for m in (medications or [])]
        med_names = [m for m in med_names if m]
        allergy_names = [_normalize_allergy(a) for a in (allergies or [])]
        allergy_names = [a for a in allergy_names if a]
        results: list[ClinicalRuleResult] = []

        # Drug-drug interactions — only when both knowledge clients are
        # available, otherwise we skip rather than touch the network.
        if (
            self.rxnorm_client is not None
            and self.openfda_client is not None
            and len(med_names) >= 2
        ):
            ddi_results = await check_drug_interactions(
                med_names,
                rxnorm=self.rxnorm_client,
                openfda=self.openfda_client,
            )
            for ddi in ddi_results:
                results.append(self._ddi_result(ddi))

        # Allergy cross-reactivity — locally decidable, always runs.
        # 使用结构化警告以支持严重度分级（同类直接过敏=contraindicated，
        # 跨类如青霉素-头孢=warning，造影剂/乳胶/神经肌肉阻滞剂=critical）。
        for drug in med_names:
            for warning in get_allergy_cross_reactivity_warnings(drug, allergy_names):
                results.append(
                    ClinicalRuleResult(
                        rule_type=ClinicalRuleType.ALLERGY,
                        severity=warning["severity"],
                        finding=(
                            f"药物 {drug} 与患者过敏原 {warning['allergen']} "
                            f"存在交叉反应风险（{warning['cross_reactive_drug']}）"
                        ),
                        affected_resources=[drug],
                        recommendation=(f"避免使用 {drug}；如确需使用，须先经过敏专科评估"),
                    )
                )

        # Duplicate therapy — locally decidable, always runs.
        for med_a, med_b in _detect_duplicate_therapy(med_names):
            results.append(
                ClinicalRuleResult(
                    rule_type=ClinicalRuleType.DUPLICATE_THERAPY,
                    severity="warning",
                    finding=f"重复治疗：{med_a} 与 {med_b} 疑似同活性成分",
                    affected_resources=[med_a, med_b],
                    recommendation="复核用药方案，避免重复治疗",
                )
            )

        # RxNorm-aware duplicate therapy — runs when an RxNorm client is
        # available. Catches brand↔generic equivalents the local detector
        # misses (Tylenol vs Acetaminophen, Vicodin vs Norco). Pairs already
        # flagged by the local detector are de-duplicated.
        if self.rxnorm_client is not None and len(med_names) >= 2:
            try:
                rxnorm_dupes = await _detect_duplicate_therapy_rxnorm(med_names, self.rxnorm_client)
            except Exception:  # noqa: BLE001 — RxNorm failures never break the safety floor
                rxnorm_dupes = []
            already_flagged = {
                (_normalize_drug_base(a), _normalize_drug_base(b))
                for a, b in _detect_duplicate_therapy(med_names)
            }
            for med_a, med_b, ingredient in rxnorm_dupes:
                key = (
                    _normalize_drug_base(med_a),
                    _normalize_drug_base(med_b),
                )
                if key in already_flagged:
                    continue
                results.append(
                    ClinicalRuleResult(
                        rule_type=ClinicalRuleType.DUPLICATE_THERAPY,
                        severity="warning",
                        finding=(
                            f"重复治疗：{med_a} 与 {med_b} 含同一活性成分 "
                            f"({ingredient})（RxNorm 等价识别）"
                        ),
                        affected_resources=[med_a, med_b],
                        recommendation="复核用药方案，避免重复治疗",
                    )
                )

        return results

    @staticmethod
    def _ddi_result(ddi: DrugInteractionResult) -> ClinicalRuleResult:
        severity = _DDI_SEVERITY_MAP.get(ddi.severity, "warning")
        management = ddi.management or "评估替代方案或加强不良反应监测"
        return ClinicalRuleResult(
            rule_type=ClinicalRuleType.DRUG_INTERACTION,
            severity=severity,
            finding=(f"{ddi.drug_a} 与 {ddi.drug_b} 存在 {ddi.severity} 级药物相互作用"),
            affected_resources=[ddi.drug_a, ddi.drug_b],
            recommendation=management,
        )

    # ── orchestration ──────────────────────────────────────────────────

    async def evaluate_all(self, patient_context: dict) -> list[ClinicalRuleResult]:
        """Run every applicable rule against a patient context.

        ``patient_context`` may contain ``vitals`` (dict), ``labs`` (list of
        dicts), ``medications`` (list[str]) and ``allergies`` (list[str]).
        """
        results: list[ClinicalRuleResult] = []
        vitals = patient_context.get("vitals") or {}
        if vitals:
            results.extend(self.evaluate_vitals(vitals))
        labs = patient_context.get("labs") or []
        if labs:
            results.extend(self.evaluate_labs(labs))
        medications = patient_context.get("medications") or []
        if medications:
            results.extend(
                await self.evaluate_medications(medications, patient_context.get("allergies") or [])
            )
        return results

    @staticmethod
    def get_blocking_findings(
        results: list[ClinicalRuleResult],
    ) -> list[ClinicalRuleResult]:
        """Return findings that must be acknowledged by a clinician.

        Blocking = ``critical`` or ``contraindicated``. Lower-severity
        findings are advisory only.
        """
        return [r for r in results if r.severity in ("critical", "contraindicated")]
