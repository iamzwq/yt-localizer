from app.subtitle.segment import (
    format_subtitles,
    is_quality_poor,
    process_subtitles,
)


def _words(pairs):
    """从 (text, start, end) 元组构造词级事件。"""
    return [{"text": t, "start": s, "end": e} for t, s, e in pairs]


def test_process_subtitles_breaks_on_sentence_punctuation():
    events = _words(
        [
            ("Hello", 0, 500),
            ("world.", 500, 1000),
            ("Next", 1100, 1500),
            ("one.", 1500, 2000),
        ]
    )
    cues = process_subtitles(events)
    assert [c["text"] for c in cues] == ["Hello world.", "Next one."]
    assert cues[0]["start"] == 0
    assert cues[0]["end"] == 1000


def test_process_subtitles_breaks_on_long_silence():
    events = _words(
        [
            ("a", 0, 500),
            ("b", 500, 1000),
            # 与上一词间隔 > 1000ms，触发超时断句。
            ("c", 2500, 3000),
        ]
    )
    cues = process_subtitles(events)
    assert [c["text"] for c in cues] == ["a b", "c"]


def test_process_subtitles_starts_with_sign_breaks():
    events = _words(
        [
            ("hello", 0, 500),
            ("[music]", 600, 1000),
        ]
    )
    cues = process_subtitles(events)
    assert [c["text"] for c in cues] == ["hello", "[music]"]


def test_format_subtitles_cjk_breaks_on_length_and_punct():
    # 中文无空格，按标点或 30 字长度断句。
    events = _words(
        [
            ("你好", 0, 500),
            ("世界。", 500, 1000),
            ("下一句", 1100, 1600),
        ]
    )
    cues = format_subtitles(events, "zh-CN")
    assert cues[0]["text"] == "你好世界。"
    assert cues[0]["end"] == 1000


def test_format_subtitles_splits_long_sentence():
    # 一个超过阈值、无句末标点的长句应二次拆分。
    events = _words(
        [(w, i * 300, i * 300 + 250) for i, w in enumerate(("word",) * 40)]
    )
    cues = format_subtitles(events, "en", long_sentence_threshold=50)
    assert len(cues) > 1


def test_format_subtitles_splits_on_word_count_without_punctuation():
    """无标点、字符数未超阈值但词数超限（>15 词）的长句也应二次拆分。

    首次扫描时词数上限依赖逗号不生效，这里验证二次拆分阶段按词数触发。
    """
    # 20 个 "one"：字符数 79 < 100，词数 20 > 15，仅词数维度触发。
    events = _words(
        [(w, i * 100, i * 100 + 90) for i, w in enumerate(("one",) * 20)]
    )
    cues = format_subtitles(events, "en")
    assert len(cues) > 1
    assert max(len(c["text"].split()) for c in cues) <= 15


def test_format_subtitles_keeps_short_word_count_sentence():
    """12 词无标点句子字符数/词数均未超限，保持单条不切。"""
    events = _words(
        [(w, i * 100, i * 100 + 90) for i, w in enumerate(("one",) * 12)]
    )
    cues = format_subtitles(events, "en")
    assert len(cues) == 1
    assert len(cues[0]["text"].split()) == 12


def test_is_quality_poor():
    good = _words([("short", 0, 1)])
    assert not is_quality_poor(good, 5, 0.5)
    poor = _words([("this line is quite long", 0, 1)])
    assert is_quality_poor(poor, 5, 0.5)
