"""规则断句（唯一策略）。

从 kiss-translator 的 processSubtitles / formatSubtitles / isQualityPoor
精确移植。输入词级 flat_events，输出句子级 cues ``[{start, end, text}]``。

- 空格分隔语言（英/欧）：状态机断句，超长句二次拆分。
- 无空格语言（中/日/韩/泰等）：按标点 + 静音 + 长度上限断句。
"""

import re
from typing import Any, Dict, List, Optional

from .prepare import FlatEvent

Cue = Dict[str, Any]  # {"start": int|float, "end": int|float, "text": str}

_WS_SPLIT_RE = re.compile(r"\s+")

# 空格语言：句末结束标点 / 逗号 / 起始符号。
_END_OF_SENTENCE_RE = re.compile(r"[.?!…\]\)]$")
_PAUSE_OF_SENTENCE_RE = re.compile(r",$")
_STARTS_WITH_SIGN_RE = re.compile(r"^[\[\(♪]")
# 无空格语言：句末标点后允许若干闭合引号/括号再结束。
_CJK_END_OF_SENTENCE_RE = re.compile("[。！？.!?…][”’\"'」』】）》）\\]]*$")

_NO_SPACE_LANGUAGES = ("zh", "ja", "ko", "th", "lo", "km", "my")

# 逻辑连词词库，仅用于英文/空格语言的 usePause 二次拆分。
_PAUSE_WORDS = frozenset(
    {
        "actually", "also", "although", "and", "anyway", "as", "basically",
        "because", "but", "eventually", "frankly", "honestly", "hopefully",
        "however", "if", "instead", "it's", "just", "let's", "like",
        "literally", "maybe", "meanwhile", "nevertheless", "nonetheless",
        "now", "okay", "or", "otherwise", "perhaps", "personally", "probably",
        "right", "since", "so", "suddenly", "that's", "then", "there's",
        "therefore", "though", "thus", "unless", "until", "well", "while",
    }
)


def _word_count(text: str) -> int:
    return len([w for w in _WS_SPLIT_RE.split(text) if w])


def process_subtitles(
    flat_events: List[FlatEvent],
    use_pause: bool = False,
    timeout: int = 1000,
    max_words: int = 15,
    max_duration_ms: int = 10000,
) -> List[Cue]:
    """空格语言核心断句状态机。"""
    sentences: List[Cue] = []
    current_buffer: List[FlatEvent] = []
    buffer_word_count = 0

    def flush() -> None:
        nonlocal current_buffer, buffer_word_count
        if current_buffer:
            sentences.append(
                {
                    "text": " ".join(s["text"] for s in current_buffer).strip(),
                    "start": current_buffer[0]["start"],
                    "end": current_buffer[-1]["end"],
                }
            )
        current_buffer = []
        buffer_word_count = 0

    for segment in flat_events:
        if not segment.get("text"):
            continue

        if current_buffer:
            last = current_buffer[-1]
            is_end_of_sentence = bool(_END_OF_SENTENCE_RE.search(last["text"]))
            is_pause_of_sentence = bool(_PAUSE_OF_SENTENCE_RE.search(last["text"]))
            is_timeout = segment["start"] - last["end"] > timeout
            is_duration_exceeded = (
                segment["start"] - current_buffer[0]["start"] >= max_duration_ms
            )
            is_word_limit_exceeded = (
                use_pause or is_pause_of_sentence
            ) and buffer_word_count >= max_words
            starts_with_sign = bool(_STARTS_WITH_SIGN_RE.search(segment["text"]))
            starts_with_pause_word = (
                use_pause
                and segment["text"].lower().split(" ")[0] in _PAUSE_WORDS
                and len(current_buffer) > 1
            )

            if (
                is_end_of_sentence
                or is_timeout
                or is_duration_exceeded
                or is_word_limit_exceeded
                or starts_with_sign
                or starts_with_pause_word
            ):
                flush()

        current_buffer.append(segment)
        buffer_word_count += _word_count(segment["text"])

    flush()
    return sentences


def is_quality_poor(
    lines: List[Dict[str, Any]],
    length_threshold: int = 200,
    percentage_threshold: float = 0.1,
) -> bool:
    """长行占比过高时判定源字幕排版质量较差，应停止自动合并分段。"""
    if not lines:
        return False
    long_lines = sum(1 for line in lines if len(line["text"]) > length_threshold)
    return long_lines / len(lines) > percentage_threshold


def format_subtitles(
    flat_events: List[FlatEvent],
    lang: Optional[str],
    long_sentence_threshold: int = 100,
    long_sentence_max_words: int = 15,
) -> List[Cue]:
    """规则断句主入口，按语言特性自适应分段。

    ``long_sentence_threshold`` 默认 100，与原插件设置项一致（原纯函数默认 120）。
    ``long_sentence_max_words`` 默认 15，与 ``process_subtitles`` 的词数上限一致。

    词数维度是二次拆分的补充触发条件：无标点 ASR 文本在首次扫描时词数上限不
    生效（``process_subtitles`` 的 ``is_word_limit_exceeded`` 依赖逗号/use_pause），
    导致 15-20 词的长句整条保留。这里在二次拆分阶段补上：字符数超限或词数超限
    任一成立即触发 ``use_pause=True`` 的语义二次拆分（在连词处切，比硬切更自然）。
    """
    if not flat_events:
        return []

    if lang and any(lang.startswith(code) for code in _NO_SPACE_LANGUAGES):
        subtitles: List[Cue] = []

        if is_quality_poor(flat_events, 5, 0.5):
            return list(flat_events)

        current_line: Optional[Cue] = None
        max_length = 30
        pause_threshold_ms = 1000

        for segment in flat_events:
            if segment.get("text"):
                # 无标点字幕遇到明显静音先结束上一句，避免跨越长停顿合并。
                if current_line and segment["start"] - current_line["end"] > pause_threshold_ms:
                    subtitles.append(current_line)
                    current_line = None

                if not current_line:
                    current_line = {
                        "text": segment["text"],
                        "start": segment["start"],
                        "end": segment["end"],
                    }
                else:
                    current_line["text"] += segment["text"]
                    current_line["end"] = segment["end"]

                is_end = bool(_CJK_END_OF_SENTENCE_RE.search(segment["text"]))
                if is_end or len(current_line["text"]) >= max_length:
                    subtitles.append(current_line)
                    current_line = None
            elif current_line:
                subtitles.append(current_line)
                current_line = None

        if current_line:
            subtitles.append(current_line)

        return subtitles

    subtitles = process_subtitles(flat_events)

    result: List[Cue] = []
    for sub in subtitles:
        # 字符数或词数超限都视为长句，触发 use_pause=True 的二次语义拆分。
        if (
            len(sub["text"]) > long_sentence_threshold
            or _word_count(sub["text"]) > long_sentence_max_words
        ):
            sub_events = [
                e for e in flat_events if sub["start"] <= e["start"] < sub["end"]
            ]
            if len(sub_events) > 1:
                result.extend(process_subtitles(sub_events, use_pause=True))
            else:
                result.append(sub)
        else:
            result.append(sub)

    return result
