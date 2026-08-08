"""可选的 AI 断句模式（默认关闭，需要调用方显式开启）。

设计要点：
- 断句边界完全由大模型判断（更懂语义/无标点 ASR 文本），但只让模型返回切分点
  编号，不要求它复述原文或给译文——原文/时间戳始终由本模块基于原始词级
  ``flat_events`` 重建，翻译交给调用方统一跑一次 ``translate_cues``。
  这样单次请求的生成量从"整段原文+译文"降到"几个整数"，大幅降低 token 与耗时。
- 每个分块严格校验：切分点必须是输入范围内、严格递增的整数；任何一步校验失败，
  该分块（或未覆盖的尾部）自动降级为现有规则断句 ``format_subtitles``，
  失败上限就是规则断句的现状，不会更差。
- 代价：不再逐字比对模型是否"数对了词"，只做结构校验（递增、不越界、覆盖到底）。
  出现极少见的"切分点差一个词"不会再被检测出来，用可靠性换取大幅降本增速。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

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
    "Each segment must not exceed 15 words; aim for 8-12 words when a natural boundary exists."
)
_NO_SPACE_LENGTH_RULE = (
    "This language has no spaces between words; each segment must not exceed 30 source characters."
)

_PROMPT_TEMPLATE = """You are a subtitle segmentation assistant for tech/programming video \
transcripts. Input is a word-level JSON array: each element is {"id": <int>, "text": "<word>", \
"pauseMs": <optional gap in ms after this word>}, ids start at 0 and strictly increase.

Task: split the transcript into semantically complete, subtitle-friendly segments.

Output contract (strict):
- Output ONLY a JSON array of integers (no quotes, not strings): each integer is the id of the
  LAST word in a segment, in strictly increasing order; the first segment starts at id 0.
- No code fences, no explanations, no prefix/suffix, no original text or translation.
- Every id must be covered exactly once: the last integer must equal the input's maximum id.
- Length limit: __LENGTH_RULE__
- If a sentence exceeds the limit, split at a clause/comma/conjunction/phrase boundary; the
  limit takes priority over sentence completeness.
- Do not merge two complete sentences into one segment; terminal punctuation usually ends one.
- Do not split technical terms, code identifiers, CLI commands, file paths, or product names
  (keep e.g. "machine learning", "npm install", "kubectl apply -f").
- "pauseMs" is the gap in ms after this word; a missing field means no notable pause — do not
  infer one. Larger values suggest a stronger boundary, but grammar and semantics always win.
- Double check before returning: ids strictly increasing and the array covers the max id.

Example:
Input: [{"id":0,"text":"Hello"},{"id":1,"text":"world."},{"id":2,"text":"This"},{"id":3,"text":"is"},{"id":4,"text":"next."}]
Output: [1, 4] (segment 1 covers id 0-1, segment 2 covers id 2-4)"""


def _is_no_space_lang(lang: Optional[str]) -> bool:
    return bool(lang) and any(lang.startswith(code) for code in _NO_SPACE_LANGUAGES)


def build_ai_segment_messages(
    indexed_events: List[Dict[str, Any]], lang: Optional[str]
) -> List[Dict[str, str]]:
    """构造 AI 断句请求的对话消息。"""
    length_rule = _NO_SPACE_LENGTH_RULE if _is_no_space_lang(lang) else _SPACE_LENGTH_RULE
    system = _PROMPT_TEMPLATE.replace("__LENGTH_RULE__", length_rule)
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


def parse_ai_segments(content: Optional[str]) -> Optional[List[int]]:
    """解析 AI 断句响应为切分点整数数组；格式不合法（非数组/元素非整数）返回 None。"""
    if not content:
        return None
    data = _try_json_array(_strip_code_fence(str(content)))
    if data is None:
        return None

    cutpoints: List[int] = []
    for raw in data:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            cutpoints.append(raw)
        elif isinstance(raw, float) and raw.is_integer():
            cutpoints.append(int(raw))
        else:
            return None
    return cutpoints


def _join_span_text(span: List[FlatEvent], lang: Optional[str]) -> str:
    texts = [ev["text"] for ev in span]
    return "".join(texts) if _is_no_space_lang(lang) else " ".join(texts)


def _build_cues_from_cutpoints(
    cutpoints: List[int], source_events: List[FlatEvent], lang: Optional[str]
) -> "tuple[List[Cue], int]":
    """校验切分点合法递增，返回已验证的 cue 列表与最后覆盖到的下标。"""
    cues: List[Cue] = []
    prev_e = -1
    for e in cutpoints:
        start_idx = prev_e + 1
        if e < start_idx or e >= len(source_events):
            break

        span = source_events[start_idx : e + 1]
        cues.append(
            {
                "start": span[0]["start"],
                "end": span[-1]["end"],
                "text": _join_span_text(span, lang),
            }
        )
        prev_e = e

    return cues, prev_e


_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def _request_cutpoints(
    events_for_request: List[FlatEvent],
    call_llm: CallLLM,
    lang: Optional[str],
    max_retries: int,
) -> Optional[List[int]]:
    """请求 AI 切分点。

    - 网络/接口异常：按 ``_RETRY_BACKOFF_SECONDS`` 指数退避重试，最多
      ``max_retries`` 次（默认 ``temperature=0`` 下解析失败重试无效，因此
      解析失败不重试，直接返回 ``None`` 交给调用方降级）。
    """
    indexed = build_indexed_events(events_for_request)
    messages = build_ai_segment_messages(indexed, lang)
    for attempt in range(max(1, max_retries)):
        try:
            content = call_llm(messages)
        except Exception:  # noqa: BLE001 - 网络/接口异常触发退避重试
            if attempt + 1 < max(1, max_retries):
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
            continue
        return parse_ai_segments(content)
    return None


def _process_chunk(
    chunk: List[FlatEvent],
    call_llm: CallLLM,
    lang: Optional[str],
    max_retries: int,
    tail_retry: bool = False,
) -> List[Cue]:
    cutpoints = _request_cutpoints(chunk, call_llm, lang, max_retries)
    if cutpoints is None:
        return format_subtitles(chunk, lang)

    cues, covered_end = _build_cues_from_cutpoints(cutpoints, chunk, lang)
    if covered_end == len(chunk) - 1:
        return cues

    remainder_start = covered_end + 1
    remainder = chunk[remainder_start:]
    leftover_start = 0
    if tail_retry:
        # 未覆盖到分块末尾：只对剩余部分重试一次，再退回规则断句兜底。
        # 默认关闭：temperature=0 的确定性模型重试结果基本不变，纯浪费一次请求。
        tail_cutpoints = _request_cutpoints(remainder, call_llm, lang, max_retries)
        if tail_cutpoints is not None:
            tail_cues, tail_covered_end = _build_cues_from_cutpoints(
                tail_cutpoints, remainder, lang
            )
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
    max_chunk_chars: int = 2000,
    max_retries: int = 3,
    temperature: float = 0.0,
    timeout: int = 180,
    concurrency: int = 8,
    tail_retry: Optional[bool] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Cue]:
    """AI 断句主入口：只产出 ``{start, end, text}``，不含翻译。

    逐块校验切分点合法性，失败部分自动降级为规则断句。翻译由调用方对返回的
    cues 统一跑一次 ``translate_cues``。分块之间互不依赖，用 ``concurrency``
    个线程并发请求缩短多分块视频的总耗时；``progress(done, total)`` 按完成
    顺序（而非提交顺序）回调，用于上报断句进度。

    ``tail_retry`` 控制"切分点未覆盖到分块末尾时，对剩余部分补一次请求"：
    ``None``（默认）时由 ``temperature`` 决定——``temperature=0`` 的确定性
    模型补试结果基本不变，纯浪费，故关闭；``temperature>0`` 时保留该兜底。
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

    if tail_retry is None:
        tail_retry = temperature > 0

    chunks = chunk_events(flat_events, max_chunk_chars)
    total = len(chunks)
    if total == 0:
        return []

    def _one(i: int) -> Any:
        return i, _process_chunk(chunks[i], call_llm, lang, max_retries, tail_retry)

    results: List[Optional[List[Cue]]] = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(_one, i) for i in range(total)]
        for future in as_completed(futures):
            i, chunk_cues = future.result()
            results[i] = chunk_cues
            done += 1
            if progress:
                progress(done, total)

    cues: List[Cue] = []
    for chunk_cues in results:
        cues.extend(chunk_cues or [])
    return cues

