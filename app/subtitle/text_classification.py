"""字幕文本分类：识别纯非语音说明片段（如 [Music]、[Applause]）。

从 kiss-translator 的 subtitleTextClassification.js 精确移植。
"""

import re

# 纯方括号字幕通常是音乐、笑声等声音说明；可选的 `>>` 是 YouTube 的说话人切换前缀。
# 只匹配整段，避免误删 "Use [React] in this project" 一类正常对白。
_NON_SPEECH_SEGMENT_RE = re.compile(r"^(?:>>\s*)?(?:\[[^\]\r\n]+\]\s*)+$")


def is_non_speech_segment(text: str = "") -> bool:
    """判断整段文本是否完全由非语音说明组成。"""
    return bool(_NON_SPEECH_SEGMENT_RE.match(str(text).strip()))
