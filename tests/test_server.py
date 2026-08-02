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
