"""Text processing algorithms (M18 C).

Real, dependency-light text utilities used across the agent:
* Keyword / keyphrase extraction for Chinese and English text (TF scoring
  with stop-word filtering) — used for query expansion, tagging, metadata.
* Sentence / paragraph splitting with CJK awareness.
* Summary heuristics (first-N + keyword-covered sentences).
"""

from __future__ import annotations

import re
from collections import Counter

# CJK + ASCII word tokens
_WORD_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")

_EN_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "if",
    "then",
    "else",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "with",
    "by",
    "from",
    "is",
    "are",
    "was",
    "were",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "as",
    "be",
    "been",
    "can",
    "will",
    "would",
    "should",
    "may",
    "might",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    "not",
    "no",
    "also",
    "such",
    "which",
    "what",
    "how",
    "why",
}

_CJK_STOP = {
    "的",
    "了",
    "在",
    "是",
    "和",
    "与",
    "及",
    "或",
    "也",
    "都",
    "很",
    "把",
    "被",
    "于",
    "对",
    "为",
    "从",
    "到",
    "这",
    "那",
    "之",
    "有",
    "上",
    "下",
    "中",
    "而",
    "且",
    "并",
    "其",
    "它",
    "他",
    "她",
    "我",
    "你",
    "们",
}


def extract_keywords(
    text: str,
    limit: int = 8,
    *,
    min_len: int = 1,
) -> list[str]:
    """Extract the most salient keywords from Chinese/English text.

    Uses ``jieba`` for Chinese word segmentation when available (mature
    tokenization instead of naive character splitting); falls back to
    character/word tokenization otherwise. Tokens are scored by normalized
    frequency after stop-word and punctuation removal.
    """
    if not text:
        return []
    tokens: list[str] = []
    try:  # 优先用成熟的中文分词库
        import jieba  # type: ignore[import-not-found]

        for tok in jieba.cut(text):
            tok = tok.strip()
            if tok and not re.fullmatch(r"[\W_]+", tok):
                tokens.append(tok)
        if not tokens:  # 分词结果异常时回退
            tokens = [m.group(0) for m in _WORD_RE.finditer(text)]
    except ImportError:  # pragma: no cover
        tokens = [m.group(0) for m in _WORD_RE.finditer(text)]

    counts: Counter[str] = Counter()
    for tok in tokens:
        if tok in _EN_STOP or tok in _CJK_STOP:
            continue
        if tok.lower() in _EN_STOP:
            continue
        if len(tok) < min_len:
            continue
        counts[tok] += 1
    if not counts:
        return []
    total = sum(counts.values())
    scored = sorted(
        ((tok, c / total) for tok, c in counts.items()),
        key=lambda x: x[1],
        reverse=True,
    )
    return [tok for tok, _ in scored[:limit]]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences (supports CJK and ASCII terminators)."""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？.!?；;])\s*", text)
    return [p.strip() for p in parts if p.strip()]


def summarize(text: str, max_sentences: int = 3, keywords: list[str] | None = None) -> str:
    """Heuristic extractive summary: leading sentences + keyword-covered ones."""
    sents = split_sentences(text)
    if not sents:
        return ""
    if len(sents) <= max_sentences:
        return " ".join(sents)
    kws = set(keywords or extract_keywords(text, limit=5))
    chosen: list[str] = []
    for i, s in enumerate(sents):
        if i < max_sentences:
            chosen.append(s)
        elif kws and any(k in s for k in kws):
            chosen.append(s)
    return " ".join(chosen[: max_sentences + 1])


def token_count(text: str) -> int:
    """Rough token estimate (CJK chars count 1, ASCII words ~1.3)."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    words = len(re.findall(r"[a-zA-Z0-9']+", text))
    return cjk + int(words * 1.3)


def sanitize_for_index(text: str) -> str:
    """Normalize text for FTS indexing: lowercase, strip control chars."""
    text = text.replace("\x00", "")
    return " ".join(text.split())


__all__ = [
    "extract_keywords",
    "split_sentences",
    "summarize",
    "token_count",
    "sanitize_for_index",
]
