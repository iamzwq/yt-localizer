"""本地 FastAPI 服务：串联字幕/视频流水线并托管前端页面。

- POST /api/prepare      下载视频+字幕、断句、翻译 → 返回 cues 与视频地址
- GET  /api/srt/{job}    下载 SRT（原文/译文/双语）
- POST /api/video/{job}  烧录视频（原声版 / 配音版）
- /media/*               静态托管工作目录（视频、成片）
- /                      前端页面
"""

import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ass import SubtitleStyle
from .burn import burn_subtitles
from .fetch import SubtitleNotFoundError, fetch_subtitle
from .srt import MODE_BILINGUAL, MODE_ORIGINAL, MODE_TRANSLATED, build_srt
from .subtitle import format_subtitles, prepare_timed_text_events
from .translate import translate_cues
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
    mode: str = "original"  # original 原声 / dub 配音
    sub_mode: str = MODE_TRANSLATED  # 烧录字幕：translated 中文 / bilingual 双语
    style: StyleModel = StyleModel()


def _job_or_404(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job


@app.post("/api/prepare")
def prepare(req: PrepareRequest):
    import hashlib

    job_id = hashlib.sha1(req.url.encode("utf-8")).hexdigest()[:12]
    job_dir = os.path.join(WORKSPACE, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        downloaded = download_video(req.url, output=os.path.join(job_dir, "source.%(ext)s"))
        fetched = fetch_subtitle(req.url, req.lang)
    except SubtitleNotFoundError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"下载失败：{err}")

    prepared = prepare_timed_text_events(fetched.events)
    cues = format_subtitles(prepared.flat_events, fetched.lang)

    warning = None
    if req.translate:
        try:
            translate_cues(cues, model=req.model or "deepseek-chat")
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

    return {
        "job_id": job_id,
        "title": fetched.title,
        "lang": fetched.lang,
        "kind": fetched.kind,
        "duration": downloaded.duration or fetched.duration,
        "video_url": f"/media/{job_id}/{os.path.basename(downloaded.path)}",
        "cues": cues,
        "warning": warning,
    }


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
    cues = job["cues"]
    job_dir = job["dir"]

    if req.sub_mode not in (MODE_TRANSLATED, MODE_BILINGUAL, MODE_ORIGINAL):
        raise HTTPException(status_code=400, detail="非法的 sub_mode")

    try:
        if req.mode == "dub":
            burned = burn_subtitles(
                job["video"],
                cues,
                os.path.join(job_dir, "_burned.mp4"),
                style=style,
                mode=req.sub_mode,
            )
            duration_ms = int((job["meta"].get("duration") or 0) * 1000) or None
            dub = build_dub_track(
                cues, os.path.join(job_dir, "dub.m4a"), total_duration_ms=duration_ms
            )
            out = mux_dub_video(burned, dub, os.path.join(job_dir, "video_dub.mp4"))
        else:
            out = burn_subtitles(
                job["video"],
                cues,
                os.path.join(job_dir, "video_original.mp4"),
                style=style,
                mode=req.sub_mode,
            )
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"合成失败：{err}")

    return {"video_url": f"/media/{job_id}/{os.path.basename(out)}"}


app.mount("/media", StaticFiles(directory=WORKSPACE), name="media")
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
