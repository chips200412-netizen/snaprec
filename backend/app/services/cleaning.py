from __future__ import annotations

import re
from collections import Counter


class ProtectedTokenError(ValueError):
    """Cleaning removed a token that can materially change meaning."""


_PROTECTED_PATTERNS = [
    r"\d+(?:[.,]\d+)?",  # numbers, dates split into numeric components
    r"[¥￥$€]\s*\d+(?:[.,]\d+)?",
    r"\d+(?:元|万元|亿元|美元|天|年|月|日|小时|分钟|秒|%|％|个|次|步)",
    r"(?:如果|只要|除非|必须|需要|前提|条件|否则|不能|不要|没有|并非|不|未|第[一二三四五六七八九十\d]+步)",
]


def protected_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for pattern in _PROTECTED_PATTERNS:
        tokens.extend(match.group(0) for match in re.finditer(pattern, text))
    return tokens


def clean_transcript(text: str) -> str:
    """Conservatively remove filler and exact adjacent repetitions."""
    cleaned = re.sub(r"(?<!\w)(?:嗯|呃|额|就是|那个)(?!\w)[，,、 ]*", "", text)
    cleaned = re.sub(r"([。！？!?])\1+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    before = Counter(protected_tokens(text))
    after = Counter(protected_tokens(cleaned))
    missing = list((before - after).elements())
    if missing:
        raise ProtectedTokenError(f"cleaning removed protected tokens: {missing!r}")
    return cleaned
