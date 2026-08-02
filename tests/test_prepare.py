from app.subtitle import prepare_timed_text_events
from app.subtitle.clean import clean_timed_text
from app.subtitle.text_classification import is_non_speech_segment


def test_clean_timed_text_strips_tags_zero_width_and_whitespace():
    assert clean_timed_text("<b>hello</b>\u200b  world ") == "hello world"
    assert clean_timed_text(None) == ""
    assert clean_timed_text("  a\n b  ") == "a b"


def test_is_non_speech_segment():
    assert is_non_speech_segment("[Music]")
    assert is_non_speech_segment(">> [Applause]")
    assert is_non_speech_segment("[Music] [Applause]")
    # 正常对白中的方括号不应被误判。
    assert not is_non_speech_segment("Use [React] in this project")
    assert not is_non_speech_segment("hello")


def test_prepare_flattens_words_with_timeline():
    events = [
        {
            "tStartMs": 1000,
            "dDurationMs": 2000,
            "segs": [
                {"utf8": "Once", "tOffsetMs": 0},
                {"utf8": " the", "tOffsetMs": 400},
                {"utf8": " assets", "tOffsetMs": 800},
            ],
        }
    ]
    prepared = prepare_timed_text_events(events)
    texts = [e["text"] for e in prepared.flat_events]
    assert texts == ["Once", "the", "assets"]
    assert prepared.flat_events[0]["start"] == 1000
    assert prepared.flat_events[1]["start"] == 1400
    # 最后一个词以整个事件结束时间为准。
    assert prepared.flat_events[-1]["end"] == 3000


def test_prepare_removes_adjacent_duplicate_events():
    events = [
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello", "tOffsetMs": 0}]},
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello", "tOffsetMs": 0}]},
    ]
    prepared = prepare_timed_text_events(events)
    assert len(prepared.events) == 1


def test_prepare_filters_non_speech_markers():
    events = [
        {
            "tStartMs": 0,
            "dDurationMs": 2000,
            "segs": [
                {"utf8": "[Music]", "tOffsetMs": 0},
                {"utf8": "hello", "tOffsetMs": 1000},
            ],
        }
    ]
    prepared = prepare_timed_text_events(events)
    assert prepared.filtered_non_speech_count == 1
    assert [e["text"] for e in prepared.flat_events] == ["hello"]


def test_prepare_handles_empty_input():
    prepared = prepare_timed_text_events(None)
    assert prepared.flat_events == []
    assert prepared.events == []
