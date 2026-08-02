"""timedtext 片段文本清洗。

从 kiss-translator 的 cleanTimedText 精确移植：去 HTML 标签、去 U+200B
零宽空格、去首尾空白、压缩内部空白。
"""

import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH_SPACE = "\u200b"


def clean_timed_text(utf8: str = "") -> str:
    """清洗单个 timedtext 片段，返回可展示与断句的纯文本。"""
    if utf8 is None:
        return ""
    text = _TAG_RE.sub("", str(utf8))
    # 仅移除 U+200B，保留 U+200C/U+200D 等对部分文字成形有意义的字符。
    text = text.replace(_ZERO_WIDTH_SPACE, "")
    text = text.strip()
    text = _WS_RE.sub(" ", text)
    return text
