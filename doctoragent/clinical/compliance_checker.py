"""合规检查服务。
检测用户缺失的资质，返回需要完成的合规事项说明。
在系统首次使用和定期检查时调用。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

__all__ = [
    "ComplianceItem",
    "ComplianceChecker",
    "get_compliance_checker",
]


class ComplianceItem(BaseModel):
    """单项合规要求。"""

    id: str  # 唯一标识，如 "nmpa"
    name: str  # 名称，如 "NMPA医疗器械认证"
    category: str  # 分类: "must" (必须) / "recommended" (建议) / "conditional" (条件性)
    status: str  # "not_started" / "in_progress" / "completed" / "not_required"
    description: str  # 简短描述
    legal_basis: str  # 法律依据
    tutorial_path: str = ""  # 教程文档路径（可选）
    applicable_scenario: str  # 适用场景说明
    estimated_duration: str  # 预计周期
    authority: str  # 办理机构
    risk_if_missing: str  # 不做的风险
    action_steps: list[str]  # 操作步骤摘要


class ComplianceChecker:
    """合规检查器。"""

    # 所有合规要求定义
    ITEMS = [
        ComplianceItem(
            id="nmpa",
            name="NMPA 医疗器械认证",
            category="must",
            status="not_started",
            description="CDS Hooks + 用药安全提醒功能可能属于二类医疗器械范畴",
            legal_basis="《医疗器械监督管理条例》《人工智能医用软件分类界定指导原则》",
            applicable_scenario="产品涉及临床决策支持、用药安全提醒、危急值预警等功能时必须取得",
            estimated_duration="12-18个月",
            authority="国家药品监督管理局（NMPA）/ 省级药监局",
            risk_if_missing="未取得医疗器械注册证即销售/使用属违法，面临没收违法所得、罚款、吊销营业执照",
            action_steps=[
                "1. 委托医疗器械注册咨询机构进行分类界定",
                "2. 确定分类（预期为二类）",
                "3. 准备产品技术要求和说明书",
                "4. 完成临床评价（同品种比对）",
                "5. 建立质量管理体系（GMP）",
                "6. 提交注册申请",
                "7. 接受技术审评",
                "8. 取得注册证",
            ],
        ),
        ComplianceItem(
            id="algorithm_filing",
            name="生成式 AI 算法备案",
            category="must",
            status="not_started",
            description="使用大模型生成临床建议，属于生成式AI服务，上线前必须完成算法备案",
            legal_basis="《互联网信息服务深度合成管理规定》《生成式人工智能服务管理暂行办法》",
            applicable_scenario="产品使用LLM生成鉴别诊断、SOAP病历、用药建议等内容时必须备案",
            estimated_duration="2-4个月",
            authority="国家互联网信息办公室（网信办）",
            risk_if_missing="未备案即上线属违法，面临警告、罚款、责令关闭服务、吊销许可证",
            action_steps=[
                "1. 完成算法安全评估（偏见/安全/隐私）",
                "2. 编写算法机制说明文件",
                "3. 准备安全评估报告",
                "4. 在网信办备案系统提交申请",
                "5. 配合审核",
                "6. 取得备案号",
                "7. 在产品页面公示备案信息",
            ],
        ),
        ComplianceItem(
            id="dengbao3",
            name="网络安全等级保护三级",
            category="must",
            status="not_started",
            description="医疗机构核心信息系统要求等保三级，是医院采购的硬门槛",
            legal_basis="《网络安全法》《网络安全等级保护条例》",
            applicable_scenario="产品部署在医院核心信息系统环境时必须取得等保三级备案",
            estimated_duration="4-6个月",
            authority="公安机关网络安全保卫部门",
            risk_if_missing="无法进入医院采购流程；已部署的系统面临限期整改、罚款",
            action_steps=[
                "1. 系统定级（确定为等保三级）",
                "2. 在公安机关备案",
                "3. 委托测评机构进行差距分析",
                "4. 完成安全整改（技术+管理）",
                "5. 等级测评",
                "6. 取得备案证明",
                "7. 每年定期测评",
            ],
        ),
        ComplianceItem(
            id="irb",
            name="IRB 伦理审查",
            category="conditional",
            status="not_started",
            description="涉及患者数据的临床验证必须先获得伦理委员会批准",
            legal_basis="《涉及人的生物医学研究伦理审查办法》《药物临床试验质量管理规范》",
            applicable_scenario="开展涉及患者数据的临床验证（包括回顾性研究）时必须取得",
            estimated_duration="1-3个月",
            authority="医院伦理委员会 / 区域伦理委员会",
            risk_if_missing="未经伦理审查开展临床研究属违规，研究数据不可用，面临学术和法律责任",
            action_steps=[
                "1. 准备研究方案",
                "2. 撰写知情同意书（或申请豁免）",
                "3. 提交伦理审查申请",
                "4. 伦理委员会审查",
                "5. 获得批准（或修改后重审）",
                "6. 研究过程中跟踪报告",
            ],
        ),
        ComplianceItem(
            id="data_security",
            name="数据安全合规（三法）",
            category="must",
            status="not_started",
            description="个人信息保护法、数据安全法、网络安全法合规，医疗数据属敏感个人信息",
            legal_basis="《个人信息保护法》《数据安全法》《网络安全法》《医疗健康数据安全指南》",
            applicable_scenario="产品处理任何患者数据时必须合规",
            estimated_duration="1-2个月",
            authority="网信办 / 公安部 / 行业主管部门",
            risk_if_missing="违法处理个人信息最高罚款5000万元或上一年度营业额5%",
            action_steps=[
                "1. 完成数据分类分级",
                "2. 开展个人信息保护影响评估（PIA）",
                "3. 制定数据处理规范和权限矩阵",
                "4. 如有数据出境，完成出境安全评估",
                "5. 建立数据安全应急预案",
                "6. 定期开展数据安全教育培训",
            ],
        ),
        ComplianceItem(
            id="hipaa",
            name="HIPAA 合规（国际市场）",
            category="conditional",
            status="not_started",
            description="如产品面向美国市场或处理美国患者数据，需满足HIPAA合规",
            legal_basis="Health Insurance Portability and Accountability Act (HIPAA)",
            applicable_scenario="产品面向美国市场、处理PHI、或与美国医疗机构合作时需要",
            estimated_duration="3-6个月",
            authority="美国卫生与公众服务部民权办公室（HHS OCR）",
            risk_if_missing="HIPAA违规罚款100美元至50000美元/次，年度上限150万美元/类",
            action_steps=[
                "1. 签署BAA协议",
                "2. 完成安全风险评估",
                "3. 实施管理/物理/技术保障措施",
                "4. 员工培训",
                "5. 制定违规通报流程",
                "6. 聘请第三方审计",
            ],
        ),
    ]

    def __init__(self, storage_path: Path | None = None):
        """初始化，加载已保存的合规状态。"""
        # 用 JSON 文件存储合规状态，默认 ~/.doctoragent/compliance_status.json
        if storage_path is None:
            storage_path = Path.home() / ".doctoragent" / "compliance_status.json"
        self.storage_path = Path(storage_path)
        # 内存中的状态覆盖层：item_id -> {"status": ..., "notes": ...}
        self._overrides: dict[str, dict[str, Any]] = {}
        self._load()

    # ── 持久化 ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载已保存的合规状态覆盖层。"""
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._overrides = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
            logger.warning("读取合规状态文件失败 %s: %s", self.storage_path, exc)
            self._overrides = {}

    def _save(self) -> None:
        """将合规状态覆盖层写入磁盘。"""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(
                json.dumps(self._overrides, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001
            logger.warning("写入合规状态文件失败 %s: %s", self.storage_path, exc)

    # ── 内部工具 ─────────────────────────────────────────────────────────

    def _resolved_items(self) -> list[ComplianceItem]:
        """返回合并了用户覆盖状态后的合规项列表。"""
        resolved: list[ComplianceItem] = []
        for item in self.ITEMS:
            override = self._overrides.get(item.id)
            if override and "status" in override:
                # 复制一份并应用覆盖状态，避免污染类常量
                merged = item.model_dump()
                merged["status"] = override["status"]
                resolved.append(ComplianceItem(**merged))
            else:
                resolved.append(item)
        return resolved

    def _find_item(self, item_id: str) -> ComplianceItem | None:
        """根据 id 查找合规项（使用合并后的状态）。"""
        return next((i for i in self._resolved_items() if i.id == item_id), None)

    # ── 公共 API ─────────────────────────────────────────────────────────

    def check_compliance(self) -> dict:
        """检查所有合规项状态。"""
        items = self._resolved_items()
        must_items = [i for i in items if i.category == "must"]
        must_completed = [i for i in must_items if i.status == "completed"]
        must_missing = [i for i in must_items if i.status not in ("completed", "not_required")]
        recommended_items = [i for i in items if i.category == "recommended"]
        conditional_items = [i for i in items if i.category == "conditional"]
        return {
            "items": [i.model_dump() for i in items],
            "summary": {
                "total": len(items),
                "must_total": len(must_items),
                "must_completed": len(must_completed),
                "must_missing": len(must_missing),
                "recommended_total": len(recommended_items),
                "conditional_total": len(conditional_items),
            },
        }

    def update_status(self, item_id: str, status: str, notes: str = "") -> bool:
        """更新某项合规状态。"""
        if not any(i.id == item_id for i in self.ITEMS):
            return False
        self._overrides[item_id] = {"status": status, "notes": notes}
        self._save()
        return True

    def get_missing_items(self) -> list[ComplianceItem]:
        """获取所有未完成的必须项。"""
        return [
            i
            for i in self._resolved_items()
            if i.category == "must" and i.status not in ("completed", "not_required")
        ]

    def get_blocking_items(self) -> list[ComplianceItem]:
        """获取阻断落地的合规项（必须但未完成）。"""
        return self.get_missing_items()

    def get_compliance_summary(self) -> dict:
        """获取合规摘要（用于前端展示）。"""
        items = self._resolved_items()
        must_items = [i for i in items if i.category == "must"]
        must_completed = [i for i in must_items if i.status == "completed"]
        must_missing = [i for i in must_items if i.status not in ("completed", "not_required")]
        completion_rate = len(must_completed) / len(must_items) if must_items else 0.0
        if not must_items:
            overall_status = "not_applicable"
        elif not must_missing:
            overall_status = "compliant"
        elif len(must_completed) > 0:
            overall_status = "partial"
        else:
            overall_status = "non_compliant"
        return {
            "overall_status": overall_status,
            "completion_rate": round(completion_rate, 4),
            "must_total": len(must_items),
            "must_completed": len(must_completed),
            "must_missing": len(must_missing),
            "blocking_items": [i.model_dump() for i in must_missing],
            "total_items": len(items),
        }

    def should_show_warning(self) -> bool:
        """是否应该显示合规警告（有必须项未完成）。"""
        return len(self.get_missing_items()) > 0

    def get_warning_message(self) -> str:
        """获取合规警告消息（展示给用户的提示文案）。"""
        missing = self.get_missing_items()
        if not missing:
            return ""
        names = "、".join(i.name for i in missing)
        return (
            f"检测到 {len(missing)} 项必须合规资质尚未完成（{names}）。"
            "未完成相关合规要求前，产品不得正式商用部署，请尽快前往「合规中心」查看教程并办理。"
        )


# ── 模块级单例 ────────────────────────────────────────────────────────────

_default_checker: ComplianceChecker | None = None


def get_compliance_checker() -> ComplianceChecker:
    """获取默认的合规检查器单例。"""
    global _default_checker
    if _default_checker is None:
        _default_checker = ComplianceChecker()
    return _default_checker
