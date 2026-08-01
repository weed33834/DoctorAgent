"""多模态内容提取器与 Classifier 集成测试。

测试不依赖真实 pypdf/tesseract/whisper：依赖缺失场景通过 ``patch`` 模拟。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from doctoragent.api.schemas import ClassificationResult
from doctoragent.connections.models import Connection, PlatformType
from doctoragent.model import ModelProvider
from doctoragent.model.classifier import Classifier
from doctoragent.model.extractors.audio_extractor import AudioContentExtractor
from doctoragent.model.extractors.base import (
    MAX_EXTRACTION_CHARS,
    ContentExtractor,
    ExtractionResult,
)
from doctoragent.model.extractors.image_extractor import ImageContentExtractor
from doctoragent.model.extractors.manager import ExtractionManager
from doctoragent.model.extractors.text_extractor import TextContentExtractor

# ── 辅助类 ────────────────────────────────────────────────────────────────


class RecordingProvider(ModelProvider):
    """记录 ``chat_completion`` 入参消息的 fake provider。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.captured_messages: list[dict[str, Any]] = []

    async def chat_completion(self, messages: list[dict[str, Any]]) -> str:
        self.captured_messages = [dict(m) for m in messages]
        return self.response

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class StubExtractor(ContentExtractor):
    """返回固定 ``ExtractionResult`` 的测试提取器。"""

    def __init__(self, text: str, method: str = "text") -> None:
        self._text = text
        self._method = method

    def supports(self, path: Path) -> bool:
        return True

    def extract(self, path: Path, max_chars: int = MAX_EXTRACTION_CHARS) -> ExtractionResult:
        return ExtractionResult(
            text=self._text,
            method=self._method,
            char_count=len(self._text),
        )


# 一份始终能被分类器解析为合法 ClassificationResult 的 fake 响应。
_VALID_RESPONSE = (
    '{"sensitivity": "low", "category": "documents", '
    '"tags": ["note"], "summary": "A note", '
    '"disguise_name": "file1234", "disguise_extension": "log"}'
)


@pytest.fixture
def local_connection() -> Connection:
    """受信任的本地连接，供 Classifier 构造使用。"""
    return Connection(
        name="Local Test",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_local=True,
    )


# ── TextContentExtractor ─────────────────────────────────────────────────


def test_text_extractor_txt(tmp_path: Path) -> None:
    """纯文本文件可被提取，method 为 text。"""
    sample = tmp_path / "note.txt"
    sample.write_text("hello world", encoding="utf-8")
    extractor = TextContentExtractor()
    result = extractor.extract(sample)
    assert result.text == "hello world"
    assert result.method == "text"
    assert result.char_count == 11
    assert not result.truncated
    assert result.mime_type == "text/plain"


def test_text_extractor_truncation(tmp_path: Path) -> None:
    """超长文本被截断且 truncated=True。"""
    sample = tmp_path / "long.txt"
    sample.write_text("a" * 500, encoding="utf-8")
    extractor = TextContentExtractor()
    result = extractor.extract(sample, max_chars=100)
    assert result.method == "text"
    assert result.truncated
    assert result.char_count == 100
    assert result.text == "a" * 100


def test_text_extractor_unsupported(tmp_path: Path) -> None:
    """不支持的扩展名 supports 返回 False。"""
    extractor = TextContentExtractor()
    assert not extractor.supports(tmp_path / "file.bin")
    assert not extractor.supports(tmp_path / "file.mp3")
    assert not extractor.supports(tmp_path / "file.png")


def test_text_extractor_pdf_missing_dep(tmp_path: Path) -> None:
    """pypdf 未安装时 PDF 提取降级返回 method="none"。"""
    sample = tmp_path / "fake.pdf"
    sample.write_bytes(b"%PDF-1.4\n%fake\n")
    extractor = TextContentExtractor()
    with patch(
        "doctoragent.model.extractors.text_extractor._import_pypdf",
        return_value=None,
    ):
        result = extractor.extract(sample)
    assert result.method == "none"
    assert result.text == ""
    assert result.mime_type == "application/pdf"


# ── ImageContentExtractor ────────────────────────────────────────────────


def test_image_extractor_missing_dep(tmp_path: Path) -> None:
    """pytesseract 未安装时 OCR 降级返回 method="none"。"""
    sample = tmp_path / "scan.png"
    sample.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG header
    extractor = ImageContentExtractor()
    with patch(
        "doctoragent.model.extractors.image_extractor._import_pytesseract",
        return_value=None,
    ):
        result = extractor.extract(sample)
    assert result.method == "none"
    assert result.text == ""
    assert result.mime_type == "image/png"


# ── AudioContentExtractor ────────────────────────────────────────────────


def test_audio_extractor_missing_dep(tmp_path: Path) -> None:
    """whisper 未安装时 ASR 降级返回 method="none"。"""
    sample = tmp_path / "voice.mp3"
    sample.write_bytes(b"ID3")  # 伪 MP3 头
    extractor = AudioContentExtractor()
    with patch(
        "doctoragent.model.extractors.audio_extractor._import_whisper",
        return_value=(None, None),
    ):
        result = extractor.extract(sample)
    assert result.method == "none"
    assert result.text == ""
    assert result.mime_type == "audio/mpeg"


# ── ExtractionManager ────────────────────────────────────────────────────


def test_extraction_manager_routing(tmp_path: Path) -> None:
    """按扩展名路由到正确提取器。"""
    manager = ExtractionManager()
    # txt → TextContentExtractor
    txt = tmp_path / "a.txt"
    txt.write_text("hello", encoding="utf-8")
    assert manager.supports(txt)
    result = manager.extract(txt)
    assert result.method == "text"
    assert result.text == "hello"
    # png → ImageContentExtractor（依赖缺失会降级到 none）
    png = tmp_path / "b.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert manager.supports(png)
    # mp3 → AudioContentExtractor
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"ID3")
    assert manager.supports(mp3)


def test_extraction_manager_no_match(tmp_path: Path) -> None:
    """未知扩展名返回 method="none"。"""
    manager = ExtractionManager()
    unknown = tmp_path / "x.unknownext"
    unknown.write_bytes(b"binary")
    assert not manager.supports(unknown)
    result = manager.extract(unknown)
    assert result.method == "none"
    assert result.text == ""
    assert result.mime_type is None


def test_extraction_manager_custom_extractors(tmp_path: Path) -> None:
    """可注入自定义提取器列表，由其负责处理。"""
    stub = StubExtractor("custom content")
    manager = ExtractionManager([stub])
    sample = tmp_path / "anything.xyz"
    sample.write_bytes(b"data")
    assert manager.supports(sample)
    result = manager.extract(sample)
    assert result.text == "custom content"
    assert result.method == "text"


# ── ExtractionResult 字段 ─────────────────────────────────────────────────


def test_extraction_result_fields() -> None:
    """ExtractionResult 字段完整且默认值正确。"""
    result = ExtractionResult(
        text="abc",
        method="text",
        mime_type="text/plain",
        char_count=3,
        truncated=False,
    )
    assert result.text == "abc"
    assert result.method == "text"
    assert result.mime_type == "text/plain"
    assert result.char_count == 3
    assert not result.truncated
    # 默认值
    default = ExtractionResult(text="", method="none")
    assert default.mime_type is None
    assert default.char_count == 0
    assert not default.truncated


# ── Classifier 集成 ──────────────────────────────────────────────────────


async def test_classifier_with_extractor(
    tmp_path: Path,
    local_connection: Connection,
) -> None:
    """mock extractor，验证提取文本进入 LLM prompt。"""
    sample = tmp_path / "notes.txt"
    sample.write_text("placeholder", encoding="utf-8")
    extractor = ExtractionManager([StubExtractor("SECRET_TOKEN_42")])
    provider = RecordingProvider(_VALID_RESPONSE)
    classifier = Classifier(provider, local_connection, extractor=extractor)
    result = await classifier.classify(sample)
    assert isinstance(result, ClassificationResult)
    # 验证提取的文本进入 user message。
    assert provider.captured_messages, "provider 未捕获消息"
    user_msg = provider.captured_messages[1]["content"]
    assert "文件内容片段" in user_msg
    assert "SECRET_TOKEN_42" in user_msg


async def test_classifier_without_extractor(
    tmp_path: Path,
    local_connection: Connection,
) -> None:
    """extractor=None 时行为不变（向后兼容）。"""
    sample = tmp_path / "notes.txt"
    sample.write_text("placeholder", encoding="utf-8")
    provider = RecordingProvider(_VALID_RESPONSE)
    classifier = Classifier(provider, local_connection)  # extractor 默认 None
    result = await classifier.classify(sample)
    assert isinstance(result, ClassificationResult)
    assert provider.captured_messages
    user_msg = provider.captured_messages[1]["content"]
    assert "文件内容片段" not in user_msg


async def test_classifier_extractor_empty_text_skipped(
    tmp_path: Path,
    local_connection: Connection,
) -> None:
    """extractor 返回空文本时不加入 prompt（降级行为）。"""
    sample = tmp_path / "notes.txt"
    sample.write_text("placeholder", encoding="utf-8")
    extractor = ExtractionManager([StubExtractor("", method="none")])
    provider = RecordingProvider(_VALID_RESPONSE)
    classifier = Classifier(provider, local_connection, extractor=extractor)
    await classifier.classify(sample)
    user_msg = provider.captured_messages[1]["content"]
    assert "文件内容片段" not in user_msg
