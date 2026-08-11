# mypy: ignore-errors
"""Tests for clinical specialty roles + built-in knowledge seed."""

from __future__ import annotations

from pathlib import Path

import pytest

from doctoragent.clinical.knowledge import KNOWLEDGE_DOCS, list_knowledge, seed_knowledge
from doctoragent.clinical.roles import default_role, get_role, list_roles


def test_builtin_roles_cover_key_specialties() -> None:
    roles = list_roles()
    codes = {r.code for r in roles}
    # 覆盖各身份医生
    for c in ("general", "cardiology", "surgery", "anesthesia", "emergency",
              "icu", "pediatrics", "obgyn", "neurology", "respiratory",
              "endocrinology", "oncology", "nephrology", "gastroenterology",
              "psychiatry", "laboratory", "radiology", "pharmacy"):
        assert c in codes, f"missing role {c}"


def test_role_fields_present() -> None:
    for r in list_roles():
        assert r.prompt, f"{r.code} missing prompt"
        assert r.scope, f"{r.code} missing scope"
        assert r.disclaimer
        assert r.focus


def test_get_and_default_role() -> None:
    assert get_role("cardiology") is not None
    assert get_role("nope") is None
    assert default_role().code == "general"


def test_cardiology_content() -> None:
    card = get_role("cardiology")
    assert any("胸痛" in f for f in card.red_flags)
    assert any("心衰" in f or "心力衰竭" in f for f in card.focus)


def test_builtin_knowledge_nonempty() -> None:
    assert len(KNOWLEDGE_DOCS) >= 10
    items = list_knowledge()
    assert all("topic" in i and "content" in i for i in items)
    # 关键主题应存在
    topics = {i["topic"] for i in items}
    for t in ("危急值速查", "药物相互作用速查", "生命体征与检验参考范围", "急性冠脉综合征初步处理"):
        assert t in topics, f"missing knowledge topic {t}"


def test_seed_knowledge_writes_md(tmp_path: Path) -> None:
    n = seed_knowledge(tmp_path)
    assert n == len(KNOWLEDGE_DOCS)
    # 二次调用不重复写（overwrite=False）
    n2 = seed_knowledge(tmp_path)
    assert n2 == 0
    files = list((tmp_path / "临床知识").glob("*.md"))
    assert len(files) == len(KNOWLEDGE_DOCS)
    # 内容有临床实质
    critical = (tmp_path / "临床知识" / "危急值速查.md").read_text(encoding="utf-8")
    assert "血钾" in critical
