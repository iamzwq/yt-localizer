"""本地 FastAPI 服务：串联字幕/视频流水线并托管前端页面。

- POST /api/prepare      下载视频+字幕、断句、翻译 → 返回 cues 与视频地址
- GET  /api/srt/{job}    下载 SRT（原文/译文/双语）
- POST /api/video/{job}  烧录视频（原声版 / 配音版）
- /media/*               静态托管工作目录（视频、成片）
- /                      前端页面
"""

import dataclasses
import json
import os
import queue
import threading
from typing import Iterator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ass import SubtitleStyle
from .burn import burn_subtitles
from .fetch import SubtitleNotFoundError, fetch_subtitle
from .srt import MODE_BILINGUAL, MODE_ORIGINAL, MODE_TRANSLATED, build_srt
from .subtitle import format_subtitles, prepare_timed_text_events
from .translate import DEFAULT_MODEL, translate_cues
from .tts import build_dub_track, mux_dub_video
from .video import download_video

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(BASE_DIR, "workspace")
WEB_DIR = os.path.join(BASE_DIR, "web")
os.makedirs(WORKSPACE, exist_ok=True)

# 内存态任务表：job_id -> {"dir", "video", "cues", "meta"}
_JOBS: dict = {}

app = FastAPI(title="YT 中文化本地工具")


class PrepareRequest(BaseModel):
    url: str
    lang: Optional[str] = None
    translate: bool = True
    model: Optional[str] = None


class StyleModel(BaseModel):
    font_name: str = "LXGW WenKai Mono"
    font_size: int = 42
    text_color: str = "#FFFFFF"
    bg_color: str = "#000000"
    bg_opacity: float = 0.6
    margin_v: int = 40

    def to_style(self) -> SubtitleStyle:
        return SubtitleStyle(
            font_name=self.font_name,
            font_size=self.font_size,
            text_color=self.text_color,
            bg_color=self.bg_color,
            bg_opacity=self.bg_opacity,
            margin_v=self.margin_v,
        )


class VideoRequest(BaseModel):
    mode: str = "original"  # original 原声 / dub 配音 / both 一起导出
    sub_mode: str = MODE_TRANSLATED  # 烧录字幕：translated 中文 / bilingual 双语
    style: StyleModel = StyleModel()


def _job_or_404(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job


def _sse(q: "queue.Queue") -> Iterator[str]:
    """从队列拉取进度事件逐条推送，遇 None 结束。"""
    while True:
        item = q.get()
        if item is None:
            break
        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"


def _media_url(job_id: str, path: str) -> str:
    return f"/media/{job_id}/{os.path.basename(path)}"


def _ensure_burned(job: dict, sub_mode: str, style: SubtitleStyle, q: "queue.Queue") -> str:
    """烧录一次得到“字幕+原声”视频，相同字幕/样式时复用，避免重复烧录。

    它既是“原声版”成片，也是“配音版”换音轨的画面源。
    """
    sig = (sub_mode, dataclasses.astuple(style))
    cached = job.get("burned")
    if cached and cached["sig"] == sig and os.path.exists(cached["path"]):
        return cached["path"]
    q.put({"stage": "burn", "pct": 0})
    total_ms = int((job["meta"].get("duration") or 0) * 1000) or None
    out = burn_subtitles(
        job["video"],
        job["cues"],
        os.path.join(job["dir"], "video_original.mp4"),
        style=style,
        mode=sub_mode,
        total_duration_ms=total_ms,
        progress=lambda pct: q.put({"stage": "burn", "pct": pct}),
    )
    job["burned"] = {"sig": sig, "path": out}
    return out


@app.post("/api/prepare")
def prepare(req: PrepareRequest):
    import hashlib

    job_id = hashlib.sha1(req.url.encode("utf-8")).hexdigest()[:12]
    job_dir = os.path.join(WORKSPACE, job_id)
    os.makedirs(job_dir, exist_ok=True)

    q: "queue.Queue" = queue.Queue()

    def work() -> None:
        try:
            def hook(d: dict) -> None:
                if d.get("status") != "downloading":
                    return
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                got = d.get("downloaded_bytes") or 0
                frac = got / total if total else 0
                # bestvideo+bestaudio 会分别下载两条流、各自 0→100；
                # 按编解码信息把视频流映射到 0-90%、音频流映射到 90-100%，合成单条进度。
                info = d.get("info_dict") or {}
                v, a = info.get("vcodec"), info.get("acodec")
                has_v = bool(v) and v != "none"
                has_a = bool(a) and a != "none"
                if has_v and not has_a:
                    overall = frac * 0.9
                elif has_a and not has_v:
                    overall = 0.9 + frac * 0.1
                else:
                    overall = frac
                q.put({"stage": "download", "pct": int(overall * 100)})

            q.put({"stage": "download", "pct": 0})
            downloaded = download_video(
                req.url, output=os.path.join(job_dir, "source.%(ext)s"), progress_hook=hook
            )
            q.put({"stage": "download", "pct": 100})

            q.put({"stage": "subtitle"})
            fetched = fetch_subtitle(req.url, req.lang)

            q.put({"stage": "segment"})
            prepared = prepare_timed_text_events(fetched.events)
            cues = format_subtitles(prepared.flat_events, fetched.lang)

            warning = None
            if req.translate:
                q.put({"stage": "translate", "done": 0, "total": len(cues)})
                try:
                    translate_cues(
                        cues,
                        model=req.model or DEFAULT_MODEL,
                        progress=lambda done, total: q.put(
                            {"stage": "translate", "done": done, "total": total}
                        ),
                    )
                except ValueError as err:
                    warning = str(err)  # 缺 API Key：保留原文，前端提示

            _JOBS[job_id] = {
                "dir": job_dir,
                "video": downloaded.path,
                "cues": cues,
                "meta": {
                    "title": fetched.title,
                    "lang": fetched.lang,
                    "kind": fetched.kind,
                    "duration": downloaded.duration or fetched.duration,
                },
            }

            q.put(
                {
                    "stage": "done",
                    "result": {
                        "job_id": job_id,
                        "title": fetched.title,
                        "lang": fetched.lang,
                        "kind": fetched.kind,
                        "duration": downloaded.duration or fetched.duration,
                        "video_url": _media_url(job_id, downloaded.path),
                        "cues": cues,
                        "warning": warning,
                    },
                }
            )
        except SubtitleNotFoundError as err:
            q.put({"stage": "error", "detail": str(err)})
        except Exception as err:  # noqa: BLE001
            q.put({"stage": "error", "detail": f"下载失败：{err}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    return StreamingResponse(_sse(q), media_type="text/event-stream")


@app.get("/api/srt/{job_id}")
def get_srt(job_id: str, mode: str = MODE_TRANSLATED):
    job = _job_or_404(job_id)
    if mode not in (MODE_ORIGINAL, MODE_TRANSLATED, MODE_BILINGUAL):
        raise HTTPException(status_code=400, detail="非法的 mode")
    text = build_srt(job["cues"], mode)
    return PlainTextResponse(
        text,
        media_type="application/x-subrip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_{mode}.srt"'},
    )


@app.post("/api/video/{job_id}")
def make_video(job_id: str, req: VideoRequest):
    job = _job_or_404(job_id)
    style = req.style.to_style()

    if req.sub_mode not in (MODE_TRANSLATED, MODE_BILINGUAL, MODE_ORIGINAL):
        raise HTTPException(status_code=400, detail="非法的 sub_mode")
    if req.mode not in ("original", "dub", "both"):
        raise HTTPException(status_code=400, detail="非法的 mode")

    q: "queue.Queue" = queue.Queue()

    def work() -> None:
        try:
            # 烧录一次即可：原声版直接是它，配音版只在其上换音轨。
            burned = _ensure_burned(job, req.sub_mode, style, q)
            videos: dict = {}
            if req.mode in ("original", "both"):
                videos["original"] = _media_url(job_id, burned)
            if req.mode in ("dub", "both"):
                q.put({"stage": "tts", "done": 0, "total": len(job["cues"])})
                duration_ms = int((job["meta"].get("duration") or 0) * 1000) or None
                dub = build_dub_track(
                    job["cues"],
                    os.path.join(job["dir"], "dub.m4a"),
                    total_duration_ms=duration_ms,
                    progress=lambda done, total: q.put(
                        {"stage": "tts", "done": done, "total": total}
                    ),
                )
                q.put({"stage": "mux"})
                out = mux_dub_video(burned, dub, os.path.join(job["dir"], "video_dub.mp4"))
                videos["dub"] = _media_url(job_id, out)
            q.put({"stage": "done", "result": {"videos": videos}})
        except Exception as err:  # noqa: BLE001
            q.put({"stage": "error", "detail": f"合成失败：{err}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    return StreamingResponse(_sse(q), media_type="text/event-stream")


app.mount("/media", StaticFiles(directory=WORKSPACE), name="media")
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
