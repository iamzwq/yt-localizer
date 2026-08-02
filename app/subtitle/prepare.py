"""YouTube json3 字幕事件的清洗、去重与词级展平。

从 kiss-translator 的 prepareTimedTextEvents 精确移植。

输入：yt-dlp 下载的 json3 字幕的 ``events`` 数组（词级、带时间偏移）。
输出：
  - ``events``：清洗、相邻去重后的规范事件（保留结构，供统计类算法备用）。
  - ``flat_events``：展平、按时间单调排列的词级流 ``[{text, start, end}]``。
  - ``filtered_non_speech_count``：被过滤掉的非语音标记数量。
"""

from typing import Any, Dict, List, NamedTuple, Optional

from .clean import _WS_RE, clean_timed_text
from .text_classification import is_non_speech_segment

FlatEvent = Dict[str, Any]  # {"text": str, "start": int|float, "end": int|float}


class PreparedEvents(NamedTuple):
    events: List[Dict[str, Any]]
    flat_events: List[FlatEvent]
    filtered_non_speech_count: int


def _to_number(value: Any) -> float:
    """等价 JS ``Number(value) || 0``：无法解析或非有限时返回 0。"""
    if value is None or isinstance(value, bool):
        return 0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    if n != n or n in (float("inf"), float("-inf")):
        return 0
    return int(n) if n.is_integer() else n


def _is_finite(value: Any) -> bool:
    """等价 JS ``Number.isFinite``：数值且非 NaN/Infinity（bool 除外）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def prepare_timed_text_events(raw_events: Optional[List[Dict[str, Any]]]) -> PreparedEvents:
    events: List[Dict[str, Any]] = []
    flat_events: List[FlatEvent] = []
    filtered_non_speech_count = 0

    # buffer 暂存正在累积的当前词，直到遇到下一个断点才落入 flat_events。
    buffer: Optional[FlatEvent] = None
    last_visible_event_key = ""

    def flush_buffer(end_at: Any) -> None:
        nonlocal buffer
        if buffer is None:
            return
        if not buffer.get("end") or (_is_finite(end_at) and buffer["end"] > end_at):
            buffer["end"] = end_at
        end = buffer.get("end")
        if _is_finite(end) and end > buffer["start"]:
            flat_events.append(buffer)
        buffer = None

    for raw_event in raw_events if isinstance(raw_events, list) else []:
        event = raw_event or {}
        raw_segs = event.get("segs") if isinstance(event.get("segs"), list) else []
        t_start_ms = _to_number(event.get("tStartMs")) or 0
        d_duration_ms = _to_number(event.get("dDurationMs")) or 0
        is_line_break = (
            event.get("aAppend") == 1
            and len(raw_segs) == 1
            and (raw_segs[0] or {}).get("utf8") == "\n"
        )

        normalized_segs: List[Dict[str, Any]] = []
        for seg in raw_segs:
            seg = seg or {}
            new_seg = dict(seg)
            # 统计断句仍需识别 YouTube 的物理换行控制信号，故保留 "\n"。
            new_seg["utf8"] = "\n" if is_line_break else clean_timed_text(seg.get("utf8"))
            normalized_segs.append(new_seg)

        visible_parts = [clean_timed_text(s["utf8"]) for s in normalized_segs]
        visible_parts = [p for p in visible_parts if p]
        visible_text = _WS_RE.sub(" ", " ".join(visible_parts)).strip()
        event_key = f"{t_start_ms}|{d_duration_ms}|{visible_text}" if visible_text else ""

        # 只删除相邻且时间、时长、可见文本完全相同的重复事件（对付滚动重复）。
        if event_key and event_key == last_visible_event_key:
            continue

        canonical_event = dict(event)
        canonical_event["segs"] = normalized_segs
        events.append(canonical_event)
        last_visible_event_key = event_key

        for index, seg in enumerate(normalized_segs):
            utf8 = seg.get("utf8", "")
            t_offset_ms = _to_number(seg.get("tOffsetMs")) or 0
            text = clean_timed_text(utf8)
            start = t_start_ms + t_offset_ms

            if not text:
                # 换行控制偶尔比前一词末尾更早；这种倒退断点不能截掉未来词。
                if buffer is None or start > buffer["start"]:
                    flush_buffer(start)
                continue

            if is_non_speech_segment(text):
                # 在声音说明处结束前一语音片段，保留真实静音间隔供断句判断。
                flush_buffer(start)
                filtered_non_speech_count += 1
                continue

            flush_buffer(start)
            buffer = {"text": text, "start": start}
            if index == len(normalized_segs) - 1:
                buffer["end"] = t_start_ms + d_duration_ms

    flush_buffer(buffer.get("end") if buffer else None)

    flat_events = [
        item
        for item in flat_events
        if item
        and _is_finite(item.get("start"))
        and _is_finite(item.get("end"))
        and item["end"] > item["start"]
    ]

    return PreparedEvents(events, flat_events, filtered_non_speech_count)
