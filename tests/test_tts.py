from app.tts import build_dub_track, plan_dub_timeline


def _cues(starts):
    return [{"start": s, "end": s + 1000, "text": f"t{i}", "translation": f"译{i}"} for i, s in enumerate(starts)]


def test_plan_no_overlap_keeps_original_speed():
    cues = _cues([0, 2000, 4000])
    segments = plan_dub_timeline(cues, [1000, 1000, 1000])
    assert [s["speed"] for s in segments] == [1.0, 1.0, 1.0]
    assert [s["at"] for s in segments] == [0, 2000, 4000]
    assert [s["play_duration"] for s in segments] == [1000, 1000, 1000]


def test_plan_speeds_up_within_cap():
    # 第0句时长3000，槽位2000 → 倍率 1.5（恰好上限），播放2000。
    cues = _cues([0, 2000])
    segments = plan_dub_timeline(cues, [3000, 500], max_speedup=1.5)
    assert segments[0]["speed"] == 1.5
    assert segments[0]["play_duration"] == 2000
    assert segments[0]["at"] == 0
    assert segments[1]["at"] == 2000


def test_plan_caps_speed_and_drifts():
    # 时长5000，槽位2000 → 需2.5倍但被限到1.5，播放≈3333，下一句顺延。
    cues = _cues([0, 2000])
    segments = plan_dub_timeline(cues, [5000, 500], max_speedup=1.5)
    assert segments[0]["speed"] == 1.5
    assert segments[0]["play_duration"] == 3333
    # 上一句结束于 3333 > 2000，第二句顺延到 3333。
    assert segments[1]["at"] == 3333


def test_plan_empty_clip_advances_only_by_silence():
    cues = _cues([0, 2000])
    segments = plan_dub_timeline(cues, [0, 800])
    assert segments[0]["play_duration"] == 0
    assert segments[1]["at"] == 2000


def test_plan_last_cue_uses_source_duration_as_slot():
    cues = _cues([0])
    segments = plan_dub_timeline(cues, [1200])
    assert segments[0]["speed"] == 1.0
    assert segments[0]["play_duration"] == 1200


def test_plan_bounds_drift_with_extra_speedup():
    # 槽位1000、时长20000，1.5倍仍严重超时；在 max_drift_ms 内追加加速到硬上限 3.0，
    # 避免无界顺延导致后续字幕与配音持续错位。
    cues = _cues([0, 1000])
    segments = plan_dub_timeline(cues, [20000, 500], max_speedup=1.5, max_drift_ms=1500)
    assert segments[0]["speed"] == 3.0
    assert segments[0]["play_duration"] == 6667


def test_build_dub_track_empty_raises():
    import pytest

    with pytest.raises(Exception):
        build_dub_track([], "out.wav")
