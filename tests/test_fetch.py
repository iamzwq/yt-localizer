from app.fetch import _match_lang, select_caption_track


def _json3(url):
    return [{"ext": "srv1", "url": "x"}, {"ext": "json3", "url": url}]


def test_match_lang_exact_and_prefix():
    assert _match_lang(["en", "zh-CN"], "en") == "en"
    assert _match_lang(["en-US", "fr"], "en") == "en-US"
    assert _match_lang(["en-US"], "en-GB") == "en-US"
    assert _match_lang(["fr"], "en") is None
    assert _match_lang(["en"], None) is None


def test_select_prefers_manual_over_auto():
    info = {
        "language": "en",
        "subtitles": {"en": _json3("MANUAL")},
        "automatic_captions": {"en": _json3("AUTO")},
    }
    track = select_caption_track(info)
    assert track.url == "MANUAL"
    assert track.kind == "manual"


def test_select_uses_prefer_lang():
    info = {
        "language": "en",
        "subtitles": {"en": _json3("EN"), "ja": _json3("JA")},
        "automatic_captions": {},
    }
    track = select_caption_track(info, prefer_lang="ja")
    assert track.lang == "ja"
    assert track.url == "JA"


def test_select_falls_back_to_auto_when_no_manual():
    info = {
        "language": "en",
        "subtitles": {},
        "automatic_captions": {"en": _json3("AUTO")},
    }
    track = select_caption_track(info)
    assert track.kind == "auto"
    assert track.url == "AUTO"


def test_select_returns_none_without_json3():
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt", "url": "x"}]},
        "automatic_captions": {},
    }
    assert select_caption_track(info) is None
