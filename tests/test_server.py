import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


def _inject_job(job_id="job-test"):
    from app.server import _JOBS

    _JOBS[job_id] = {
        "dir": ".",
        "video": "x.mp4",
        "cues": [{"start": 0, "end": 1000, "text": "Hi", "translation": "嗨"}],
        "meta": {"duration": 1},
    }
    return job_id


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "YouTube" in r.text


def test_static_appjs_served(client):
    assert client.get("/app.js").status_code == 200


def test_unknown_job_returns_404(client):
    assert client.get("/api/srt/nope").status_code == 404
    assert client.post("/api/video/nope", json={"mode": "original"}).status_code == 404


def test_srt_endpoint_bilingual(client):
    job_id = _inject_job()
    r = client.get(f"/api/srt/{job_id}?mode=bilingual")
    assert r.status_code == 200
    assert "Hi" in r.text and "嗨" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")


def test_srt_endpoint_rejects_bad_mode(client):
    job_id = _inject_job()
    assert client.get(f"/api/srt/{job_id}?mode=bad").status_code == 400


def _sse_events(text):
    """解析 /api/prepare 的 SSE 响应为事件列表。"""
    import json

    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_prepare_cache_aware_of_ai_segment(monkeypatch, tmp_path):
    """切换 ai_segment 开关不命中旧缓存；同设置重复请求命中 AI 断句缓存。"""
    import hashlib

    from app import server as server_mod
    from app.fetch import FetchedSubtitle
    from app.subtitle.prepare import PreparedEvents

    url = "https://youtu.be/cache-test"
    job_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").touch()  # 预置视频文件，让缓存命中检查通过

    monkeypatch.setattr(server_mod, "WORKSPACE", str(tmp_path))

    calls = {"download": 0, "fetch": 0, "format": 0, "ai": 0}

    def fake_download(_url, output=None, progress_hook=None):
        calls["download"] += 1
        return type("D", (), {"path": "source.mp4", "duration": 1.0, "thumbnail": ""})()

    def fake_fetch(_url, prefer_lang=None):
        calls["fetch"] += 1
        return FetchedSubtitle(
            events=[{"segs": [{"utf8": "Hello"}]}],
            lang="en",
            kind="auto",
            title="T",
            description="",
            duration=1.0,
        )

    def fake_prepare(_events):
        return PreparedEvents(
            events=[],
            flat_events=[
                {"text": "Hello", "start": 0, "end": 500},
                {"text": "world.", "start": 500, "end": 1000},
            ],
            filtered_non_speech_count=0,
        )

    def fake_format(flat, lang):
        calls["format"] += 1
        return [{"start": 0, "end": 1000, "text": "Hello world."}]

    def fake_ai_format(flat, lang, **kwargs):
        calls["ai"] += 1
        return [{"start": 0, "end": 1000, "text": "Hello world."}]

    def fake_translate(cues, **kwargs):
        for c in cues:
            c["translation"] = "你好"
        return cues

    monkeypatch.setattr(server_mod, "download_video", fake_download)
    monkeypatch.setattr(server_mod, "fetch_subtitle", fake_fetch)
    monkeypatch.setattr(server_mod, "prepare_timed_text_events", fake_prepare)
    monkeypatch.setattr(server_mod, "format_subtitles", fake_format)
    monkeypatch.setattr(server_mod, "ai_format_subtitles", fake_ai_format)
    monkeypatch.setattr(server_mod, "translate_cues", fake_translate)

    # 第一次：规则断句（ai_segment=False）
    with TestClient(app) as client:
        r = client.post("/api/prepare", json={"url": url, "translate": True, "ai_segment": False})
        done = _sse_events(r.text)[-1]
        assert done["stage"] == "done"
        assert done["result"]["cached"] is False
    assert calls == {"download": 1, "fetch": 1, "format": 1, "ai": 0}

    # 第二次：切到 AI 断句——不命中旧缓存，复用视频重新断句（不重新下载）
    with TestClient(app) as client:
        r = client.post("/api/prepare", json={"url": url, "translate": True, "ai_segment": True})
        done = _sse_events(r.text)[-1]
        assert done["stage"] == "done"
        assert done["result"]["cached"] is False
    assert calls == {"download": 1, "fetch": 2, "format": 1, "ai": 1}

    # 第三次：同设置重复请求——命中 AI 断句缓存，不重新拉字幕/断句
    with TestClient(app) as client:
        r = client.post("/api/prepare", json={"url": url, "translate": True, "ai_segment": True})
        done = _sse_events(r.text)[-1]
        assert done["stage"] == "done"
        assert done["result"]["cached"] is True
    assert calls == {"download": 1, "fetch": 2, "format": 1, "ai": 1}
