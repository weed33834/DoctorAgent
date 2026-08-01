"""
本地化医学知识源配置。
减少对海外 API（openFDA/RxNorm/PubMed）的依赖，
降低数据出境风险，提升响应速度。

使用方式:
    from doctoragent.clinical.knowledge.local_sources import LocalSourceConfig
    config = LocalSourceConfig()
    config.use_local_first = True  # 优先使用本地知识源
"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "LocalSourceConfig",
    "DEFAULT_LOCAL_CONFIG",
    "get_local_source_config",
]


class LocalSourceConfig(BaseModel):
    """本地化知识源配置。"""

    # 是否优先使用本地知识源
    use_local_first: bool = True

    # 本地 DDI 知识库（已实现，在 drug_interactions.py 中）
    local_ddi_enabled: bool = True

    # 本地参考范围（已实现，在 reference_ranges.py 中）
    local_reference_ranges_enabled: bool = True

    # 本地过敏交叉反应（已实现，在 rules.py 中）
    local_allergy_rules_enabled: bool = True

    # 海外 API 配置
    openfda_enabled: bool = False  # 默认关闭，减少数据出境
    pubmed_enabled: bool = False  # 默认关闭
    rxnorm_enabled: bool = False  # 默认关闭

    # 国内替代源（预留）
    nmpa_drug_db_url: str = ""  # 国家药品监督管理局药品数据库
    wanfang_medical_url: str = ""  # 万方医学网
    cnki_medical_url: str = ""  # 知网医学

    # 本地缓存配置
    cache_enabled: bool = True
    cache_ttl_hours: int = 24

    def get_knowledge_priority(self) -> list[str]:
        """获取知识源查询优先级。"""
        priority = []
        if self.local_ddi_enabled:
            priority.append("local_ddi")
        if self.local_reference_ranges_enabled:
            priority.append("local_reference_ranges")
        if self.local_allergy_rules_enabled:
            priority.append("local_allergy_rules")
        # 海外 API 仅在明确启用时加入
        if self.openfda_enabled:
            priority.append("openfda")
        if self.pubmed_enabled:
            priority.append("pubmed")
        if self.rxnorm_enabled:
            priority.append("rxnorm")
        return priority

    def get_data_residency_report(self) -> dict:
        """生成数据驻留报告。"""
        return {
            "local_first": self.use_local_first,
            "overseas_apis_enabled": any(
                [self.openfda_enabled, self.pubmed_enabled, self.rxnorm_enabled]
            ),
            "overseas_apis": {
                "openfda": self.openfda_enabled,
                "pubmed": self.pubmed_enabled,
                "rxnorm": self.rxnorm_enabled,
            },
            "local_sources": {
                "ddi_knowledge_base": self.local_ddi_enabled,
                "reference_ranges": self.local_reference_ranges_enabled,
                "allergy_rules": self.local_allergy_rules_enabled,
            },
            "cache_enabled": self.cache_enabled,
            "data_export_risk": "low"
            if not any([self.openfda_enabled, self.pubmed_enabled, self.rxnorm_enabled])
            else "medium",
        }


# 默认配置实例
DEFAULT_LOCAL_CONFIG = LocalSourceConfig()


def get_local_source_config() -> LocalSourceConfig:
    """获取默认本地化知识源配置。"""
    return DEFAULT_LOCAL_CONFIG
