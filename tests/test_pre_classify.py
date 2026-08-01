"""Tests for pre_classify and sensitive keywords detection."""

from pathlib import Path

from doctoragent.api.schemas import SensitivityLevel
from doctoragent.model.classifier import (
    SENSITIVE_KEYWORDS,
    _match_keywords,
    pre_classify,
)

# ── SENSITIVE_KEYWORDS table ─────────────────────────────────────────────


def test_sensitive_keywords_table_structure() -> None:
    """Keywords table has expected categories."""
    expected_categories = {"identity", "finance", "legal", "medical", "credentials"}
    assert set(SENSITIVE_KEYWORDS.keys()) == expected_categories


def test_sensitive_keywords_table_has_entries() -> None:
    """Each category has at least one keyword."""
    for category, keywords in SENSITIVE_KEYWORDS.items():
        assert isinstance(keywords, list), f"{category} keywords should be a list"
        assert len(keywords) > 0, f"{category} should have keywords"


def test_keyword_table_contains_chinese_terms() -> None:
    """Keywords table includes Chinese terms."""
    identity_keywords = SENSITIVE_KEYWORDS["identity"]
    assert any("\u4e00" <= kw <= "\u9fff" for kw in identity_keywords)  # at least one CJK char


def test_keyword_table_contains_english_terms() -> None:
    """Keywords table includes English terms."""
    finance_keywords = SENSITIVE_KEYWORDS["finance"]
    assert any(kw.isascii() for kw in finance_keywords)


# ── _match_keywords ──────────────────────────────────────────────────────


def test_match_keywords_identity() -> None:
    """Filenames with identity keywords are detected."""
    category, score = _match_keywords("身份证_正面_扫描件.jpg")
    assert category == "identity"
    assert score >= 1


def test_match_keywords_passport() -> None:
    """Filenames with passport keyword are detected."""
    category, score = _match_keywords("passport_scan.png")
    assert category == "identity"
    assert score >= 1


def test_match_keywords_bank() -> None:
    """Filenames with bank keyword are detected as finance."""
    category, score = _match_keywords("银行对账单_2024.pdf")
    assert category == "finance"
    assert score >= 1


def test_match_keywords_contract() -> None:
    """Filenames with contract keyword are detected as legal."""
    category, score = _match_keywords("contract_Nda_2025.docx")
    assert category == "legal"
    assert score >= 1


def test_match_keywords_medical() -> None:
    """Filenames with medical keywords are detected."""
    category, score = _match_keywords("体检报告.pdf")
    assert category == "medical"
    assert score >= 1


def test_match_keywords_credentials() -> None:
    """Filenames with credential keywords are detected."""
    category, score = _match_keywords("api_key_secret.txt")
    assert category == "credentials"
    assert score >= 1


def test_match_keywords_no_match() -> None:
    """Filenames without keywords return None."""
    category, score = _match_keywords("random_photo.jpg")
    assert category is None
    assert score == 0


def test_match_keywords_case_insensitive() -> None:
    """Keyword matching is case-insensitive (caller pre-lowercases)."""
    category, score = _match_keywords("passport.pdf")
    assert category == "identity"


def test_match_keywords_multiple_matches() -> None:
    """Multiple keyword matches increase the score."""
    category, score = _match_keywords("passport id_card_visa扫描件.pdf")
    assert category == "identity"
    assert score >= 3


def test_match_keywords_best_category_wins() -> None:
    """When keywords from multiple categories match, the one with most matches wins."""
    category, _ = _match_keywords("身份证_银行合同.pdf")  # identity 1, finance 1, legal 1
    # With 1 match each, the first registered wins (identity).
    assert category in {"identity", "finance", "legal"}


# ── pre_classify ─────────────────────────────────────────────────────────


def make_file(tmp_path: Path, name: str, size_bytes: int = 1024) -> Path:
    """Create a test file with given name and size."""
    path = tmp_path / name
    path.write_bytes(b"\x00" * size_bytes)
    return path


def test_pre_classify_identity_document(tmp_path: Path) -> None:
    """Identity documents are pre-classified as CRITICAL/identity."""
    f = make_file(tmp_path, "身份证正面.jpg")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.CRITICAL.value
    assert result["category"] == "identity"
    assert "identity" in result["tags"]


def test_pre_classify_passport(tmp_path: Path) -> None:
    """Passport files are pre-classified as CRITICAL/identity."""
    f = make_file(tmp_path, "passport_scan.png")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.CRITICAL.value
    assert result["category"] == "identity"


def test_pre_classify_credentials(tmp_path: Path) -> None:
    """Credential files are pre-classified as CRITICAL."""
    f = make_file(tmp_path, "api_keys_secret.txt")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.CRITICAL.value
    assert result["category"] == "credentials"


def test_pre_classify_image_without_keywords(tmp_path: Path) -> None:
    """Plain images without sensitive keywords are pre-classified as LOW/media."""
    f = make_file(tmp_path, "vacation_photo.jpg")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.LOW.value
    assert result["category"] == "media"


def test_pre_classify_media_file(tmp_path: Path) -> None:
    """Media files (video/audio) are pre-classified as LOW/media."""
    f = make_file(tmp_path, "recording.mp4", size_bytes=1024 * 1024)
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.LOW.value
    assert result["category"] == "media"


def test_pre_classify_returns_none_for_unknown(tmp_path: Path) -> None:
    """Files without clear indicators return None (needs LLM)."""
    f = make_file(tmp_path, "document_draft.pdf")
    result = pre_classify(f)
    assert result is None


def test_pre_classify_bank_statement(tmp_path: Path) -> None:
    """Bank statements are pre-classified as HIGH/finance."""
    f = make_file(tmp_path, "银行对账单_2025.pdf")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.HIGH.value
    assert result["category"] == "finance"


def test_pre_classify_contract(tmp_path: Path) -> None:
    """Contracts are pre-classified as HIGH/legal."""
    f = make_file(tmp_path, "contract_NDA.docx")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.HIGH.value
    assert result["category"] == "legal"


def test_pre_classify_medical_report(tmp_path: Path) -> None:
    """Medical reports are pre-classified as HIGH/health."""
    f = make_file(tmp_path, "medical_report.pdf")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.HIGH.value
    assert result["category"] == "health"


def test_pre_classify_has_all_required_fields(tmp_path: Path) -> None:
    """Pre-classification output contains all required ClassificationResult fields."""
    f = make_file(tmp_path, "身份证.jpg")
    result = pre_classify(f)
    assert result is not None
    for field in (
        "sensitivity",
        "category",
        "tags",
        "summary",
        "disguise_name",
        "disguise_extension",
    ):
        assert field in result, f"Missing field: {field}"


def test_pre_classify_benign_extensions(tmp_path: Path) -> None:
    """Files with benign extensions and no keywords are auto-classified as benign."""
    f = make_file(tmp_path, "debug.log")
    result = pre_classify(f)
    assert result is not None
    assert result["sensitivity"] == SensitivityLevel.LOW.value
    assert result["category"] == "documents"
    assert "benign" in result["tags"]


def test_pre_classify_generates_disguise_name(tmp_path: Path) -> None:
    """Pre-classification generates a valid disguise name."""
    f = make_file(tmp_path, "passport.jpg")
    result = pre_classify(f)
    assert result is not None
    name = result["disguise_name"]
    assert isinstance(name, str)
    assert 8 <= len(name) <= 32
    assert any(c.isdigit() for c in name)  # must contain at least one digit


def test_pre_classify_picks_disguise_extension(tmp_path: Path) -> None:
    """Disguise extension is one of the neutral ones."""
    f = make_file(tmp_path, "bank_statement.pdf")
    result = pre_classify(f)
    assert result is not None
    assert result["disguise_extension"] in {"log", "txt", "csv", "dat", "bin"}


def test_pre_classify_text_disguise_is_csv(tmp_path: Path) -> None:
    """Text-related files get 'csv' as disguise extension."""
    f = make_file(tmp_path, "invoice_2025.csv")
    result = pre_classify(f)
    assert result is not None
    # invoice keyword triggers finance, text ext gets csv
    assert result["disguise_extension"] == "csv"


def test_pre_classify_missing_file(tmp_path: Path) -> None:
    """Returns None when file doesn't exist (can't stat)."""
    result = pre_classify(tmp_path / "nonexistent.pdf")
    assert result is None


def test_pre_classify_invoice(tmp_path: Path) -> None:
    """Invoice files are pre-classified as finance."""
    f = make_file(tmp_path, "invoice_202501.pdf")
    result = pre_classify(f)
    assert result is not None
    assert result["category"] == "finance"


def test_pre_classify_tags_include_size_label(tmp_path: Path) -> None:
    """Large image files include size label in tags."""
    f = make_file(tmp_path, "big_photo.jpg", size_bytes=20 * 1024 * 1024)
    result = pre_classify(f)
    assert result is not None
    assert "large" in result["tags"]


def test_pre_classify_tiny_file(tmp_path: Path) -> None:
    """Tiny image files have appropriate size label."""
    f = make_file(tmp_path, "icon.png", size_bytes=64)
    result = pre_classify(f)
    assert result is not None
    assert "tiny" in result["tags"]


def test_pre_classify_summary_no_identifiers(tmp_path: Path) -> None:
    """Pre-classified summary does not contain the original filename."""
    f = make_file(tmp_path, "passport_of_john_doe.png")
    result = pre_classify(f)
    assert result is not None
    assert "john" not in result["summary"].lower()
    assert "doe" not in result["summary"].lower()
