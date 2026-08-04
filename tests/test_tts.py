from app.tts import build_dub_track, plan_dub_timeline


def _cues(starts, window=1000):
    return [
        {"start": s, "end": s + window, "text": f"t{i}", "translation": f"译{i}"} for i, s in enumerate(starts)
    ]


def test_plan_fits_within_own_window_keeps_original_speed():
    cues = _cues([0, 2000, 4000])
    segments = plan_dub_timeline(cues, [1000, 1000, 1000])
    assert [s["speed"] for s in segments] == [1.0, 1.0, 1.0]
    assert [s["at"] for s in segments] == [0, 2000, 4000]
    assert [s["play_duration"] for s in segments] == [1000, 1000, 1000]


def test_plan_speeds_up_within_cap():
    # 第0句时长1500，自身窗口1000 → 倍率 1.5（恰好上限），播放1000。
    cues = _cues([0, 2000], window=1000)
    segments = plan_dub_timeline(cues, [1500, 500], max_speedup=1.5)
    assert segments[0]["speed"] == 1.5
    assert segments[0]["play_duration"] == 1000
    assert segments[0]["at"] == 0
    assert segments[1]["at"] == 2000


def test_plan_never_delays_next_cue_when_over_cap():
    # 时长5000，自身窗口1000，需要5倍但被限到1.5，播放≈3333（超出自己的窗口）；
    # 不做顺延：下一句依旧锚定在自己的原始 start，不受影响。
    cues = _cues([0, 2000], window=1000)
    segments = plan_dub_timeline(cues, [5000, 500], max_speedup=1.5)
    assert segments[0]["speed"] == 1.5
    assert segments[0]["play_duration"] == 3333
    assert segments[1]["at"] == 2000
    assert segments[1]["speed"] == 1.0


def test_plan_empty_clip_has_zero_play_duration():
    cues = _cues([0, 2000])
    segments = plan_dub_timeline(cues, [0, 800])
    assert segments[0]["play_duration"] == 0
    assert segments[1]["at"] == 2000


def test_plan_each_cue_independent_of_neighbors():
    # 相邻两句都超时，各自只按自己的窗口加速，互不影响、互不传导延迟。
    cues = _cues([0, 1000, 2500], window=500)
    segments = plan_dub_timeline(cues, [1000, 900, 500], max_speedup=1.6)
    assert segments[0]["speed"] == 1.6
    assert segments[0]["at"] == 0
    assert segments[1]["speed"] == 1.6
    assert segments[1]["at"] == 1000
    assert segments[2]["speed"] == 1.0
    assert segments[2]["at"] == 2500


def test_build_dub_track_empty_raises():
    import pytest

    with pytest.raises(Exception):
        build_dub_track([], "out.wav")
