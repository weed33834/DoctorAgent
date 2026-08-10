"""Data governance catalog (M20).

Models for the data-asset catalog: assets, metadata, lineage edges, quality
checks and classification. Dependency-free (pydantic only).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AssetType(StrEnum):
    DOCUMENT = "document"
    DATASET = "dataset"
    KNOWLEDGE_BASE = "knowledge_base"
    CHUNK = "chunk"
    INDEX = "index"
    MODEL = "model"


class DataSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PHI = "phi"  # protected health information


class DataAsset(BaseModel):
    id: str
    org_id: str = "default"
    name: str
    asset_type: AssetType = AssetType.DOCUMENT
    source: str = ""  # e.g. vault path / url / db
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL
    owner: str = ""
    description: str = ""
    row_count: int = 0
    size_bytes: int = 0
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LineageEdge(BaseModel):
    id: str
    upstream_asset_id: str
    downstream_asset_id: str
    transform: str = ""  # e.g. "embed", "chunk", "synthesize"
    created_at: str = ""


class QualityCheck(BaseModel):
    id: str
    asset_id: str
    check_type: str  # completeness | accuracy | freshness | duplicates
    score: float = 0.0  # 0..1
    status: str = "pass"  # pass | warn | fail
    detail: str = ""
    created_at: str = ""


class ClassificationRule(BaseModel):
    id: str
    name: str
    sensitivity: DataSensitivity
    keywords: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
