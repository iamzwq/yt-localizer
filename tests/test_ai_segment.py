import json

from app.subtitle.ai_segment import (
    ai_format_subtitles,
    build_ai_segment_messages,
    build_indexed_events,
    chunk_events,
    parse_ai_segments,
)
from app.subtitle.segment import format_subtitles


def _flat(pairs):
    """从 (text, start, end) 元组构造词级事件。"""
    return [{"text": t, "start": s, "end": e} for t, s, e in pairs]


# ---- chunk_events ----


def test_chunk_events_splits_on_char_limit():
    events = _flat([(f"word{i}", i * 200, i * 200 + 150) for i in range(10)])
    chunks = chunk_events(events, max_chars=25)
    assert sum(len(c) for c in chunks) == len(events)
    assert all(len(c) < len(events) for c in chunks)


def test_chunk_events_prefers_sentence_boundary():
    events = _flat(
        [
            ("x" * 30, 0, 500),
            ("y" * 50 + ".", 500, 1000),
            ("z" * 10, 1100, 1500),
            ("w" * 10, 1500, 2000),
        ]
    )
    # 累计长度达到 80% 预期边界且遇到句末标点时提前切块，而非等到硬上限(100)。
    chunks = chunk_events(events, max_chars=100)
    assert len(chunks) == 2
    assert len(chunks[0]) == 2
    assert chunks[0][1]["text"].endswith(".")
    assert len(chunks[1]) == 2


def test_chunk_events_empty():
    assert chunk_events([]) == []


# ---- build_indexed_events ----


def test_build_indexed_events_includes_pause():
    events = _flat([("a", 0, 500), ("b", 1600, 2000), ("c", 2000, 2500)])
    items = build_indexed_events(events)
    assert items[0] == {"id": 0, "text": "a", "pauseMs": 1100}
    assert items[1] == {"id": 1, "text": "b"}
    assert items[2] == {"id": 2, "text": "c"}


# ---- build_ai_segment_messages ----


def test_build_ai_segment_messages_space_language_rule():
    messages = build_ai_segment_messages([{"id": 0, "text": "hi"}], "en", target_lang="中文")
    assert "15" in messages[0]["content"]
    assert messages[1]["content"] == '[{"id": 0, "text": "hi"}]'


def test_build_ai_segment_messages_no_space_language_rule():
    messages = build_ai_segment_messages([{"id": 0, "text": "你"}], "zh-CN")
    assert "30" in messages[0]["content"]


def test_build_ai_segment_messages_injects_context():
    messages = build_ai_segment_messages([{"id": 0, "text": "hi"}], "en", context="视频背景X")
    assert "视频背景X" in messages[0]["content"]


# ---- parse_ai_segments ----


def test_parse_ai_segments_valid():
    content = json.dumps([{"e": 1, "o": "Hello world.", "t": "你好世界。"}])
    assert parse_ai_segments(content) == [{"e": 1, "o": "Hello world.", "t": "你好世界。"}]


def test_parse_ai_segments_strips_code_fence():
    content = '```json\n[{"e": 0, "o": "hi", "t": "嗨"}]\n```'
    assert parse_ai_segments(content) == [{"e": 0, "o": "hi", "t": "嗨"}]


def test_parse_ai_segments_rejects_non_array():
    assert parse_ai_segments('{"e": 0}') is None
    assert parse_ai_segments("not json") is None
    assert parse_ai_segments("") is None
    assert parse_ai_segments(None) is None


def test_parse_ai_segments_rejects_missing_or_wrong_type_fields():
    assert parse_ai_segments('[{"e": "0", "o": "hi", "t": "嗨"}]') is None
    assert parse_ai_segments('[{"e": 0, "o": "hi"}]') is None
    assert parse_ai_segments('[{"o": "hi", "t": "嗨"}]') is None


# ---- ai_format_subtitles ----


def _hello_world_events():
    return _flat(
        [
            ("Hello", 0, 500),
            ("world.", 500, 1000),
            ("Next", 1100, 1500),
            ("one.", 1500, 2000),
        ]
    )


def test_ai_format_subtitles_empty_input():
    assert ai_format_subtitles([], "en", call_llm=lambda m: "[]") == []


def test_ai_format_subtitles_reports_progress_per_chunk():
    events = _hello_world_events()

    def call_llm(_messages):
        return json.dumps([{"e": 1, "o": "Hello world.", "t": "你好世界。"}], ensure_ascii=False)

    calls = []
    # max_chunk_chars 设小一点，强制拆成两个分块，验证逐块回调。
    ai_format_subtitles(
        events,
        "en",
        call_llm=call_llm,
        max_chunk_chars=11,
        progress=lambda done, total: calls.append((done, total)),
    )
    assert calls == [(1, 2), (2, 2)]


def test_ai_format_subtitles_success_uses_ai_translation():
    events = _hello_world_events()

    def call_llm(_messages):
        return json.dumps(
            [
                {"e": 1, "o": "Hello world.", "t": "你好世界。"},
                {"e": 3, "o": "Next one.", "t": "下一句。"},
            ],
            ensure_ascii=False,
        )

    cues = ai_format_subtitles(events, "en", call_llm=call_llm, max_chunk_chars=10000)
    assert cues == [
        {"start": 0, "end": 1000, "text": "Hello world.", "translation": "你好世界。"},
        {"start": 1100, "end": 2000, "text": "Next one.", "translation": "下一句。"},
    ]


def test_ai_format_subtitles_falls_back_when_ai_returns_garbage():
    events = _hello_world_events()

    calls = {"n": 0}

    def call_llm(_messages):
        calls["n"] += 1
        return "not json"

    cues = ai_format_subtitles(
        events, "en", call_llm=call_llm, max_chunk_chars=10000, max_retries=2
    )
    assert cues == format_subtitles(events, "en")
    assert all("translation" not in c for c in cues)
    # 整块解析完全失败时不会再触发尾部重试，只重试 max_retries 次。
    assert calls["n"] == 2


def test_ai_format_subtitles_falls_back_when_coverage_mismatch():
    events = _hello_world_events()

    def call_llm(_messages):
        return json.dumps([{"e": 3, "o": "完全不匹配的文本", "t": "无效"}], ensure_ascii=False)

    cues = ai_format_subtitles(events, "en", call_llm=call_llm, max_chunk_chars=10000, max_retries=1)
    assert cues == format_subtitles(events, "en")
    assert all("translation" not in c for c in cues)


def test_ai_format_subtitles_partial_coverage_then_tail_succeeds():
    events = _hello_world_events()
    calls = {"n": 0}

    def call_llm(messages):
        calls["n"] += 1
        payload = json.loads(messages[1]["content"])
        if len(payload) == 4:
            return json.dumps([{"e": 1, "o": "Hello world.", "t": "你好世界。"}], ensure_ascii=False)
        return json.dumps([{"e": 1, "o": "Next one.", "t": "下一句。"}], ensure_ascii=False)

    cues = ai_format_subtitles(events, "en", call_llm=call_llm, max_chunk_chars=10000)
    assert calls["n"] == 2
    assert cues == [
        {"start": 0, "end": 1000, "text": "Hello world.", "translation": "你好世界。"},
        {"start": 1100, "end": 2000, "text": "Next one.", "translation": "下一句。"},
    ]


def test_ai_format_subtitles_partial_coverage_tail_fails_falls_back_for_remainder():
    events = _hello_world_events()

    def call_llm(messages):
        payload = json.loads(messages[1]["content"])
        if len(payload) == 4:
            return json.dumps([{"e": 1, "o": "Hello world.", "t": "你好世界。"}], ensure_ascii=False)
        return "still garbage"

    cues = ai_format_subtitles(events, "en", call_llm=call_llm, max_chunk_chars=10000, max_retries=1)
    assert cues[0] == {
        "start": 0,
        "end": 1000,
        "text": "Hello world.",
        "translation": "你好世界。",
    }
    remainder = events[2:]
    assert cues[1:] == format_subtitles(remainder, "en")
    assert all("translation" not in c for c in cues[1:])
