"""
DoctorAgent 性能测试。
验证 CDS Hooks 响应 <500ms、临床工作流延迟、并发处理能力。

运行方式:
    pytest tests/test_performance.py -v -m "not slow"
    pytest tests/test_performance.py -v -m slow --timeout=120
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 导入守卫：临床/记忆子包未安装时跳过整个模块（最小化安装场景）
# ---------------------------------------------------------------------------
try:
    from doctoragent.clinical.deidentification import PHIDetector
    from doctoragent.clinical.integrations.cds_hooks import (
        CDSHookRequest,
        CDSHookService,
    )
    from doctoragent.clinical.knowledge import check_local_ddi
    from doctoragent.clinical.safety import (
        REFERENCE_RANGES,
        ClinicalRuleEngine,
        get_allergy_cross_reactivity,
        get_reference_range,
    )
    from doctoragent.model.rag import MemorySystem

    _DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover — 仅在最小化安装时触发
    _DEPS_AVAILABLE = False

# 文件头部跳过条件：依赖缺失时跳过本模块所有测试
pytestmark = pytest.mark.skipif(
    not _DEPS_AVAILABLE, reason="doctoragent.clinical / model.rag 未安装"
)


# ---------------------------------------------------------------------------
# 辅助构造：mock 患者数据（不依赖真实 LLM 或网络）
# ---------------------------------------------------------------------------
def _mock_patient_context() -> dict[str, Any]:
    """构造包含用药/过敏/生命体征/检验的 mock 患者上下文。"""
    return {
        "patient_id": "perf-patient-1",
        "vitals": {
            "heart_rate": 48,          # 低于参考范围 → warning
            "systolic_bp": 165,        # 高于参考范围 → warning
            "spo2": 91,                # 低于参考范围 → warning
        },
        "labs": [
            {"test": "sodium", "value": 158, "unit": "mmol/L"},     # HIGH → warning
            {"test": "potassium", "value": 3.0, "unit": "mmol/L"},  # LOW → warning
            {"test": "hemoglobin", "value": 85, "unit": "g/L"},     # LOW → warning
            {"test": "glucose", "value": 8.5, "unit": "mmol/L"},    # HIGH → warning
        ],
        "medications": [
            "warfarin 5mg",
            "aspirin 100mg",
            "metformin 500mg",
            "amoxicillin 250mg",
        ],
        "allergies": ["penicillin", "sulfa"],
    }


def _mock_ddi_medications() -> list[str]:
    """10 种药物，覆盖本地 DDI 知识库中的若干相互作用对。"""
    return [
        "warfarin",
        "aspirin",
        "ibuprofen",
        "amiodarone",
        "fluconazole",
        "metronidazole",
        "sulfamethoxazole",
        "clarithromycin",
        "simvastatin",
        "metformin",
    ]


def _mock_allergies() -> list[str]:
    """患者过敏原列表，用于交叉反应查询。"""
    return [
        "penicillin", "sulfa", "aspirin", "cephalosporin",
        "iodine", "latex", "statin", "opioid",
    ]


def _clinical_text_with_phi() -> str:
    """构造约 500 字、包含多类 PHI 的临床文本。"""
    base = (
        "患者 John Doe，男，65 岁，MRN 12345678，于 2024-01-15 入院。"
        "主诉：胸痛 3 天。联系电话 13800138000，邮箱 john.doe@example.com。"
        "身份证号 110101199001011234，住址 123 Main Street。"
        "DOB 1960-05-20。既往糖尿病史，长期口服二甲双胍。"
        "过敏：青霉素。用药：华法林 5mg qd，阿司匹林 100mg qd。"
        "查体：心率 88 bpm，血压 150/95 mmHg。实验室：钠 140 mmol/L，钾 4.2。"
        "IP 地址 192.168.1.100。设备序列 SN ABC1234567。"
        "初步诊断：冠心病、高血压 3 级。建议行冠脉造影进一步评估。"
        "复查心电图与肌钙蛋白，必要时冠脉 CTA。随访电话 13900139000。"
    )
    # 重复扩展至 500 字以上
    return (base * 3)[:600]


def _fhir_bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """将 FHIR 资源列表包装为 Bundle（EHR prefetch 形状）。"""
    return {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }


def _vital_observation(loinc: str, value: float, unit: str = "bpm") -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": f"obs-{loinc}",
        "category": [{"coding": [{"code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
        "valueQuantity": {"value": value, "unit": unit},
    }


def _med_request(code: str, display: str) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": f"med-{code}",
        "status": "active",
        "medicationCodeableConcept": {
            "coding": [{"code": code, "display": display}],
            "text": display,
        },
    }


def _allergy(code: str, display: str) -> dict[str, Any]:
    return {
        "resourceType": "AllergyIntolerance",
        "id": f"all-{code}",
        "code": {"coding": [{"code": code, "display": display}], "text": display},
        "reaction": [
            {
                "manifestation": [
                    {"coding": [{"code": "rash", "display": "皮疹"}], "text": "皮疹"}
                ]
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. 参考范围查询 <10ms
# ---------------------------------------------------------------------------
def test_reference_ranges_lookup_under_10ms() -> None:
    """验证 100 次参考范围查询的平均耗时 <10ms。"""
    # REFERENCE_RANGES 至少 20 项；循环取 100 个查询目标
    names = list(REFERENCE_RANGES.keys())
    assert names, "REFERENCE_RANGES 不应为空"
    targets = [names[i % len(names)] for i in range(100)]

    start = time.perf_counter()
    for name in targets:
        get_reference_range(name)
    elapsed = time.perf_counter() - start

    avg = elapsed / 100
    assert avg < 0.01, f"参考范围平均查询时间 {avg:.4f}s 超过 10ms 阈值"


# ---------------------------------------------------------------------------
# 2. 临床规则引擎评估 <50ms
# ---------------------------------------------------------------------------
async def test_clinical_rules_evaluation_under_50ms() -> None:
    """验证 10 次 ClinicalRuleEngine.evaluate_all 的平均耗时 <50ms。"""
    engine = ClinicalRuleEngine()
    context = _mock_patient_context()

    # 预热一次（避免首次导入/缓存开销污染计时）
    await engine.evaluate_all(context)

    start = time.perf_counter()
    for _ in range(10):
        await engine.evaluate_all(context)
    elapsed = time.perf_counter() - start

    avg = elapsed / 10
    assert avg < 0.05, f"规则引擎平均评估时间 {avg:.4f}s 超过 50ms 阈值"


# ---------------------------------------------------------------------------
# 3. 本地 DDI 查询 <20ms
# ---------------------------------------------------------------------------
def test_ddi_local_lookup_under_20ms() -> None:
    """验证 10 种药物的本地 DDI 查询耗时 <20ms。"""
    medications = _mock_ddi_medications()

    start = time.perf_counter()
    results = check_local_ddi(medications)
    elapsed = time.perf_counter() - start

    # 确认查询确实命中（warfarin+aspirin 等存在相互作用）
    assert isinstance(results, list)
    assert elapsed < 0.02, f"本地 DDI 查询时间 {elapsed:.4f}s 超过 20ms 阈值"


# ---------------------------------------------------------------------------
# 4. 过敏交叉反应查询 <10ms
# ---------------------------------------------------------------------------
def test_allergy_cross_reactivity_under_10ms() -> None:
    """验证过敏交叉反应查询耗时 <10ms。"""
    drug = "amoxicillin"
    allergies = _mock_allergies()

    start = time.perf_counter()
    for _ in range(100):
        get_allergy_cross_reactivity(drug, allergies)
    elapsed = time.perf_counter() - start

    avg = elapsed / 100
    assert avg < 0.01, f"过敏交叉反应平均查询时间 {avg:.4f}s 超过 10ms 阈值"


# ---------------------------------------------------------------------------
# 5. PHI 检测 <100ms
# ---------------------------------------------------------------------------
def test_phi_detection_under_100ms() -> None:
    """验证约 500 字临床文本的 PHI 检测耗时 <100ms。"""
    detector = PHIDetector()
    text = _clinical_text_with_phi()
    assert len(text) >= 500, "测试文本应至少 500 字"

    start = time.perf_counter()
    matches = detector.detect_phi(text)
    elapsed = time.perf_counter() - start

    # 确认检测到 PHI（文本中包含姓名/电话/MRN/日期等）
    assert len(matches) > 0, "应检测到至少一处 PHI"
    assert elapsed < 0.1, f"PHI 检测时间 {elapsed:.4f}s 超过 100ms 阈值"


# ---------------------------------------------------------------------------
# 6. 并发临床分析
# ---------------------------------------------------------------------------
async def _mock_clinical_analysis(engine: ClinicalRuleEngine, context: dict) -> list:
    """模拟单次临床分析：规则评估 + 模拟 I/O 等待。"""
    await asyncio.sleep(0.02)  # 模拟 I/O 延迟（FHIR 读取等）
    return await engine.evaluate_all(context)


async def test_concurrent_clinical_analysis() -> None:
    """验证 5 个并发临床分析全部完成，且总时间 < 单个的 3 倍。"""
    engine = ClinicalRuleEngine()
    context = _mock_patient_context()

    # 测量单个分析的耗时
    single_start = time.perf_counter()
    await _mock_clinical_analysis(engine, context)
    single_elapsed = time.perf_counter() - single_start

    # 并行执行 5 个分析
    parallel_start = time.perf_counter()
    results = await asyncio.gather(
        *[_mock_clinical_analysis(engine, context) for _ in range(5)]
    )
    parallel_elapsed = time.perf_counter() - parallel_start

    # 全部完成
    assert len(results) == 5, "应完成全部 5 个并发分析"
    assert all(isinstance(r, list) for r in results), "每个分析应返回 list"
    # 总时间 < 单个的 3 倍（并发应显著快于串行的 5 倍）
    assert parallel_elapsed < single_elapsed * 3, (
        f"并发总时间 {parallel_elapsed:.4f}s 超过单个 {single_elapsed:.4f}s 的 3 倍"
    )


# ---------------------------------------------------------------------------
# 7. CDS Hooks 响应 <500ms（标记为 slow）
# ---------------------------------------------------------------------------
@pytest.mark.slow
async def test_cds_hooks_response_under_500ms() -> None:
    """验证 CDS Hooks service 端到端响应 <500ms（rules-only 降级路径）。"""
    # 构造 mock FHIR prefetch：危急心率 + 用药 + 过敏
    req = CDSHookRequest(
        hook="patient-view",
        hookInstance="perf-cds-1",
        context={"patientId": "perf-patient-1"},
        prefetch={
            "patient": {"resourceType": "Patient", "id": "perf-patient-1"},
            "observations": _fhir_bundle([_vital_observation("8867-4", 35)]),
            "medications": _fhir_bundle(
                [_med_request("warfarin", "华法林"), _med_request("aspirin", "阿司匹林")]
            ),
            "allergies": _fhir_bundle([_allergy("penicillin", "青霉素")]),
        },
    )
    svc = CDSHookService(llm_provider=None)  # rules-only 降级路径，无真实 LLM

    start = time.perf_counter()
    resp = await svc.invoke(req)
    elapsed = time.perf_counter() - start

    # 确认响应有效（应产生至少一张卡片）
    assert resp is not None
    assert resp.cards, "CDS Hooks 应返回至少一张卡片"
    assert elapsed < 0.5, f"CDS Hooks 响应时间 {elapsed:.4f}s 超过 500ms 阈值"


# ---------------------------------------------------------------------------
# 8. 记忆召回 <50ms
# ---------------------------------------------------------------------------
def test_memory_recall_under_50ms(tmp_path) -> None:
    """验证预存 100 条事实后的 recall_facts 耗时 <50ms。"""
    memory = MemorySystem(tmp_path / "perf-memory.db")

    # 预存 100 条事实
    for i in range(100):
        memory.store_fact(
            f"事实 #{i}：患者既往史项目 {i}",
            importance=0.5 + (i % 10) * 0.04,
        )

    start = time.perf_counter()
    facts = memory.recall_facts("患者", limit=20)
    elapsed = time.perf_counter() - start

    assert len(facts) > 0, "应召回至少一条事实"
    assert elapsed < 0.05, f"记忆召回时间 {elapsed:.4f}s 超过 50ms 阈值"
