from app.srt import (
    MODE_BILINGUAL,
    MODE_ORIGINAL,
    MODE_TRANSLATED,
    build_srt,
    format_timestamp,
)


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(1200) == "00:00:01,200"
    assert format_timestamp(3_723_456) == "01:02:03,456"
    # 负值截断为 0。
    assert format_timestamp(-5) == "00:00:00,000"


def test_build_srt_original():
    cues = [
        {"start": 0, "end": 1000, "text": "Hello", "translation": "你好"},
        {"start": 1000, "end": 2000, "text": "World", "translation": "世界"},
    ]
    srt = build_srt(cues, MODE_ORIGINAL)
    assert "1\n00:00:00,000 --> 00:00:01,000\nHello" in srt
    assert "你好" not in srt


def test_build_srt_translated():
    cues = [{"start": 0, "end": 1000, "text": "Hello", "translation": "你好"}]
    srt = build_srt(cues, MODE_TRANSLATED)
    assert "你好" in srt
    assert "Hello" not in srt


def test_build_srt_translated_falls_back_to_text():
    cues = [{"start": 0, "end": 1000, "text": "Hello", "translation": ""}]
    srt = build_srt(cues, MODE_TRANSLATED)
    assert "Hello" in srt


def test_build_srt_bilingual():
    cues = [{"start": 0, "end": 1000, "text": "Hello", "translation": "你好"}]
    srt = build_srt(cues, MODE_BILINGUAL)
    assert "Hello\n你好" in srt


def test_build_srt_skips_empty_cue():
    cues = [{"start": 0, "end": 1000, "text": "", "translation": ""}]
    assert build_srt(cues, MODE_ORIGINAL) == ""
