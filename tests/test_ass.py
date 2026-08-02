from app.ass import (
    SubtitleStyle,
    _ass_color,
    _ass_time,
    build_ass,
)
from app.burn import escape_ass_path_for_filter
from app.srt import MODE_BILINGUAL, MODE_ORIGINAL, MODE_TRANSLATED


def test_ass_time():
    assert _ass_time(0) == "0:00:00.00"
    assert _ass_time(1230) == "0:00:01.23"
    assert _ass_time(3_723_450) == "1:02:03.45"


def test_ass_color_opaque_white():
    assert _ass_color("#FFFFFF", 1.0) == "&H00FFFFFF"


def test_ass_color_alpha_and_channel_order():
    # #112233 → BBGGRR = 332211，半透明 alpha≈0x80。
    assert _ass_color("#112233", 0.5) == "&H80332211"


def test_ass_color_shorthand():
    assert _ass_color("#000", 1.0) == "&H00000000"


def _cues():
    return [
        {"start": 0, "end": 1000, "text": "Hello", "translation": "你好"},
        {"start": 1000, "end": 2000, "text": "World", "translation": "世界"},
    ]


def test_build_ass_contains_style_and_dialogue():
    ass = build_ass(_cues(), SubtitleStyle(font_name="LXGW WenKai Mono", font_size=40))
    assert "[V4+ Styles]" in ass
    assert "LXGW WenKai Mono,40" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.00" in ass


def test_build_ass_bilingual_uses_newline_marker():
    ass = build_ass(_cues(), mode=MODE_BILINGUAL)
    assert "Hello\\N你好" in ass


def test_build_ass_translated_only():
    ass = build_ass(_cues(), mode=MODE_TRANSLATED)
    assert "你好" in ass
    assert "Hello" not in ass


def test_build_ass_original_only():
    ass = build_ass(_cues(), mode=MODE_ORIGINAL)
    assert "Hello" in ass
    assert "你好" not in ass


def test_build_ass_escapes_braces():
    cues = [{"start": 0, "end": 1000, "text": "a{b}c", "translation": ""}]
    ass = build_ass(cues, mode=MODE_ORIGINAL)
    assert "a(b)c" in ass
    assert "{b}" not in ass.split("[Events]")[1]


def test_escape_ass_path_for_filter_windows():
    assert escape_ass_path_for_filter("D:\\a\\b.ass") == "D\\:/a/b.ass"
