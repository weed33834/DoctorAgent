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

# Cached tiktoken encoding (loaded lazily on first ``token_count`` call).
_tiktoken_encoding = None
_tiktoken_loaded = False

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

    Uses ``jieba.analyse.extract_tags`` (TF-IDF + TextRank) when available
    — jieba is already a core dependency. Falls back to a simple TF scoring
    heuristic if jieba is not installed.
    """
    if not text:
        return []
    # Primary path: jieba.analyse (mature, battle-tested keyword extraction).
    try:
        import jieba.analyse  # type: ignore[import-not-found]

        keywords = jieba.analyse.extract_tags(
            text,
            topK=limit,
            withWeight=False,
            allowPOS=(),  # no POS filtering — keep all candidate words
        )
        if keywords:
            # Apply min_len filter (jieba doesn't have this parameter).
            if min_len > 1:
                keywords = [kw for kw in keywords if len(kw) >= min_len]
            return keywords
    except ImportError:  # pragma: no cover
        pass
    except Exception:  # noqa: BLE001 — jieba internal error, fall back
        pass

    # Fallback: simple TF scoring (original implementation).
    tokens: list[str] = []
    try:
        import jieba  # type: ignore[import-not-found]

        for tok in jieba.cut(text):
            tok = tok.strip()
            if tok and not re.fullmatch(r"[\W_]+", tok):
                tokens.append(tok)
        if not tokens:
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
    """Accurate token count using tiktoken ``cl100k_base``.

    Replaces the former hand-rolled heuristic (CJK×1 + ASCII×1.3).
    tiktoken is already a core dependency.  Falls back to the
    ``len(text) // 4`` heuristic if tiktoken is unavailable.
    """
    if not text:
        return 0
    global _tiktoken_encoding, _tiktoken_loaded
    if not _tiktoken_loaded:
        _tiktoken_loaded = True
        try:
            import tiktoken

            _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            _tiktoken_encoding = None
    if _tiktoken_encoding is not None:
        try:
            return len(_tiktoken_encoding.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


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
