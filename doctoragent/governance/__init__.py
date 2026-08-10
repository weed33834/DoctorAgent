"""Data governance catalog (M20).

Real, SQLite-backed data asset management: catalog CRUD with metadata,
lineage graph, quality checks and automatic sensitivity classification
(including PHI detection). See :mod:`doctoragent.governance.store`.
"""

from __future__ import annotations

from doctoragent.governance.models import (
    AssetType,
    ClassificationRule,
    DataAsset,
    DataSensitivity,
    LineageEdge,
    QualityCheck,
)
from doctoragent.governance.store import GovernanceService, GovernanceStore

__all__ = [
    "AssetType",
    "ClassificationRule",
    "DataAsset",
    "DataSensitivity",
    "GovernanceService",
    "GovernanceStore",
    "LineageEdge",
    "QualityCheck",
]
