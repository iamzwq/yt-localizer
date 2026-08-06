"""可选的 AI 断句 + 翻译合并模式（默认关闭，需要调用方显式开启）。

设计要点：
- 断句边界完全由大模型判断（更懂语义/无标点 ASR 文本），但时间戳与原文覆盖
  始终由本模块基于原始词级 ``flat_events`` 重建，不信任模型自报的时间/原文。
- 每个分块严格校验：编号连续递增、原文逐字覆盖一致；任何一步校验失败，
  该分块（或未覆盖的尾部）自动降级为现有规则断句 ``format_subtitles``，
  失败上限就是规则断句的现状，不会更差。
- 与 ``translate_cues`` 共用 ``resolve_call_llm``；AI 断句成功的 cue 自带
  ``translation`` 字段，调用方只需对缺失该字段的 cue 再跑一次批量翻译。
"""

import json
from typing import Any, Dict, List, Optional

from ..translate import (
    CallLLM,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    _strip_code_fence,
    _try_json_array,
    resolve_call_llm,
)
from .prepare import FlatEvent
from .segment import Cue, _END_OF_SENTENCE_RE, _NO_SPACE_LANGUAGES, format_subtitles

_SPACE_LENGTH_RULE = (
    "每个片段不超过 15 个词，存在自然边界时尽量控制在 8-12 个词。"
)
_NO_SPACE_LENGTH_RULE = "该语言词间无空格，每个片段不超过 30 个源文字符。"

_PROMPT_TEMPLATE = """你是专业的字幕断句与翻译助手。输入是一段视频语音转写的词级 JSON 数组，
每个元素形如 {"id": 序号, "text": "词", "pauseMs": 该词后停顿毫秒数（可选）}，id 从 0 开始严格递增。

任务：把输入切分成语义完整、适合做字幕的片段，并把每个片段翻译成__TARGET_LANG__。

严格输出契约：
1. 只输出一个 JSON 数组，禁止 markdown 代码块围栏，禁止任何解释性文字、前后缀。
2. 数组每个元素格式：{"e": 末尾词id, "o": "该片段原文", "t": "译文"}。
3. "e" 必须是输入中真实存在、严格递增的整数 id；第一个片段从 id 0 开始覆盖，
   之后每个片段从"上一个 e + 1"开始，到当前 "e" 结束（含）。
4. 必须覆盖输入中的每个 id 恰好一次，不能遗漏、重复或跳号；最后一个 "e" 必须等于输入最大 id。
5. "o" 必须是该 id 区间原文的逐字合并结果，禁止改写、删减、增加或翻译原文内容。
6. 长度限制：__LENGTH_RULE__如果一句话超出限制，优先在从句、逗号、连词或自然短语边界拆分；
   长度限制优先于保持语法完整句子。
7. 不要把两个完整句子合并成一个片段；句末标点通常应作为片段结尾。
8. "pauseMs" 只是辅助信号，数值越大越可能是句子边界，但语义与语法完整性始终优先于停顿时长。
9. 先根据区间精确构造 "o"，再只翻译这个 "o" 得到 "t"；"t" 不能遗漏该区间内容，
   也不能包含其他片段的内容。
10. 译文要求准确、自然、简洁，符合口语字幕语境。

示例：
输入：[{"id":0,"text":"Once"},{"id":1,"text":"the"},{"id":2,"text":"assets"},{"id":3,"text":"are"},{"id":4,"text":"ready,"},{"id":5,"text":"open"},{"id":6,"text":"the"},{"id":7,"text":"storyboard"},{"id":8,"text":"tab.","pauseMs":850},{"id":9,"text":"This"},{"id":10,"text":"is"},{"id":11,"text":"where"},{"id":12,"text":"everything"},{"id":13,"text":"comes"},{"id":14,"text":"together."}]
输出：[{"e":8,"o":"Once the assets are ready, open the storyboard tab.","t":"素材准备好后，打开故事板标签页。"},{"e":14,"o":"This is where everything comes together.","t":"一切从这里开始整合。"}]"""


def _is_no_space_lang(lang: Optional[str]) -> bool:
    return bool(lang) and any(lang.startswith(code) for code in _NO_SPACE_LANGUAGES)


def build_ai_segment_messages(
    indexed_events: List[Dict[str, Any]],
    lang: Optional[str],
    target_lang: str = "中文",
    context: Optional[str] = None,
) -> List[Dict[str, str]]:
    """构造 AI 断句请求的对话消息。"""
    length_rule = _NO_SPACE_LENGTH_RULE if _is_no_space_lang(lang) else _SPACE_LENGTH_RULE
    system = _PROMPT_TEMPLATE.replace("__LENGTH_RULE__", length_rule).replace(
        "__TARGET_LANG__", target_lang
    )
    if context:
        system += f"\n\n参考背景：{context}"
    user = json.dumps(indexed_events, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_indexed_events(chunk_events: List[FlatEvent]) -> List[Dict[str, Any]]:
    """词级事件 → 带 id/pauseMs 的紧凑 JSON 结构，供 AI 断句请求使用。"""
    items: List[Dict[str, Any]] = []
    for i, event in enumerate(chunk_events):
        item: Dict[str, Any] = {"id": i, "text": event["text"]}
        if i < len(chunk_events) - 1:
            pause = chunk_events[i + 1]["start"] - event["end"]
            if pause > 0:
                item["pauseMs"] = round(pause)
        items.append(item)
    return items


def chunk_events(
    flat_events: List[FlatEvent], max_chars: int = 1000
) -> List[List[FlatEvent]]:
    """按目标字符数分块，优先在句末标点/长静音处切，减少语义跨块割裂。"""
    if not flat_events:
        return []

    max_chunk_length = max(1, max_chars)
    preferred_boundary = int(max_chunk_length * 0.8)
    pause_threshold_ms = 1000

    chunks: List[List[FlatEvent]] = []
    current: List[FlatEvent] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(current)
        current = []
        current_len = 0

    for i, event in enumerate(flat_events):
        event_len = len(str(event.get("text") or ""))
        if current and current_len + event_len > max_chunk_length:
            flush()

        current.append(event)
        current_len += event_len

        is_last = i == len(flat_events) - 1
        if not is_last and current_len >= preferred_boundary:
            is_end_of_sentence = bool(_END_OF_SENTENCE_RE.search(event["text"]))
            pause = flat_events[i + 1]["start"] - event["end"]
            if is_end_of_sentence or pause > pause_threshold_ms:
                flush()

    flush()
    return chunks


def _normalize_for_coverage(text: str) -> str:
    """归一化：只保留字母数字（含 Unicode，如 CJK），用于逐字覆盖比对。"""
    return "".join(ch.lower() for ch in text if ch.isalnum())


def parse_ai_segments(content: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """解析 AI 断句响应；格式不合法（非数组/字段缺失/类型错误）返回 None。"""
    if not content:
        return None
    data = _try_json_array(_strip_code_fence(str(content)))
    if data is None:
        return None

    items: List[Dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            return None
        e_raw, o, t = raw.get("e"), raw.get("o"), raw.get("t")
        if isinstance(e_raw, bool):
            return None
        if isinstance(e_raw, int):
            e = e_raw
        elif isinstance(e_raw, float) and e_raw.is_integer():
            e = int(e_raw)
        else:
            return None
        if not isinstance(o, str) or not isinstance(t, str):
            return None
        items.append({"e": e, "o": o, "t": t})
    return items


def _validate_and_build_cues(
    items: List[Dict[str, Any]], source_events: List[FlatEvent]
) -> "tuple[List[Cue], int]":
    """校验编号连续性 + 原文覆盖一致性，返回已验证的 cue 列表与最后覆盖到的下标。"""
    cues: List[Cue] = []
    prev_e = -1
    for item in items:
        start_idx = prev_e + 1
        end_idx = item["e"]
        if end_idx < start_idx or end_idx >= len(source_events):
            break

        span = source_events[start_idx : end_idx + 1]
        source_text = "".join(ev["text"] for ev in span)
        o_text = item["o"].strip()
        t_text = item["t"].strip()
        if not o_text or not t_text:
            break
        if _normalize_for_coverage(source_text) != _normalize_for_coverage(o_text):
            break

        cues.append(
            {
                "start": span[0]["start"],
                "end": span[-1]["end"],
                "text": o_text,
                "translation": t_text,
            }
        )
        prev_e = end_idx

    return cues, prev_e


def _request_ai_segments(
    events_for_request: List[FlatEvent],
    call_llm: CallLLM,
    lang: Optional[str],
    target_lang: str,
    context: Optional[str],
    max_retries: int,
) -> Optional[List[Dict[str, Any]]]:
    indexed = build_indexed_events(events_for_request)
    messages = build_ai_segment_messages(indexed, lang, target_lang, context)
    for _ in range(max(1, max_retries)):
        try:
            content = call_llm(messages)
        except Exception:  # noqa: BLE001 - 网络/接口异常统一触发重试
            continue
        parsed = parse_ai_segments(content)
        if parsed is not None:
            return parsed
    return None


def _process_chunk(
    chunk: List[FlatEvent],
    call_llm: CallLLM,
    lang: Optional[str],
    target_lang: str,
    context: Optional[str],
    max_retries: int,
) -> List[Cue]:
    items = _request_ai_segments(chunk, call_llm, lang, target_lang, context, max_retries)
    if items is None:
        return format_subtitles(chunk, lang)

    cues, covered_end = _validate_and_build_cues(items, chunk)
    if covered_end == len(chunk) - 1:
        return cues

    # 未覆盖到分块末尾：只对剩余部分重试一次，再退回规则断句兜底。
    remainder_start = covered_end + 1
    remainder = chunk[remainder_start:]
    tail_items = _request_ai_segments(remainder, call_llm, lang, target_lang, context, max_retries)

    leftover_start = 0
    if tail_items is not None:
        tail_cues, tail_covered_end = _validate_and_build_cues(tail_items, remainder)
        cues.extend(tail_cues)
        leftover_start = tail_covered_end + 1

    if leftover_start < len(remainder):
        cues.extend(format_subtitles(remainder[leftover_start:], lang))

    return cues


def ai_format_subtitles(
    flat_events: List[FlatEvent],
    lang: Optional[str],
    *,
    call_llm: Optional[CallLLM] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    target_lang: str = "中文",
    context: Optional[str] = None,
    max_chunk_chars: int = 1000,
    max_retries: int = 3,
    temperature: float = 0.0,
    timeout: int = 60,
) -> List[Cue]:
    """AI 断句 + 翻译合并主入口；逐块校验，失败部分自动降级为规则断句。

    成功的 cue 自带 ``translation`` 字段；降级部分没有该字段，调用方需要
    对缺失 ``translation`` 的 cue 再跑一次 ``translate_cues``。
    """
    if not flat_events:
        return []

    call_llm = resolve_call_llm(
        call_llm,
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
    )

    cues: List[Cue] = []
    for chunk in chunk_events(flat_events, max_chunk_chars):
        cues.extend(_process_chunk(chunk, call_llm, lang, target_lang, context, max_retries))
    return cues
