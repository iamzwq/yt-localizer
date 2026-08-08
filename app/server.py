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
import re
import shutil
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
from .subtitle import ai_format_subtitles, format_subtitles, prepare_timed_text_events
from .translate import DEFAULT_MODEL, build_video_context, translate_cues
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
    ai_segment: bool = False  # 用 AI 断句+翻译合并替代规则断句，仅 translate=True 时生效
    model: Optional[str] = None
    force: bool = False  # 忽略缓存强制重新下载


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
    mode: str = "original"  # original 原声 / both 一起导出（原声 + 配音）
    sub_mode: str = MODE_TRANSLATED  # 烧录字幕：translated 中文 / bilingual 双语
    style: StyleModel = StyleModel()


def _job_or_404(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job


_MANIFEST = "manifest.json"


def _manifest_path(job_dir: str) -> str:
    return os.path.join(job_dir, _MANIFEST)


def _load_manifest(job_dir: str) -> Optional[dict]:
    path = _manifest_path(job_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _save_manifest(job_dir: str, data: dict) -> None:
    with open(_manifest_path(job_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _safe_job_dir(job_id: str) -> str:
    """校验 job_id 为 hex 哈希，防止路径穿越。"""
    if not re.fullmatch(r"[0-9a-f]{6,40}", job_id or ""):
        raise HTTPException(status_code=400, detail="非法的 job_id")
    return os.path.join(WORKSPACE, job_id)


def _sse(q: "queue.Queue") -> Iterator[str]:
    """从队列拉取进度事件逐条推送，遇 None 结束。"""
    while True:
        item = q.get()
        if item is None:
            break
        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"


def _media_url(job_id: str, path: str) -> str:
    return f"/media/{job_id}/{os.path.basename(path)}"


def _dir_size(path: str) -> int:
    """递归统计目录占用字节数，跳过无法访问的文件/软链接异常。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


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


def _ensure_dub(job: dict, burned: str, sub_mode: str, style: SubtitleStyle, q: "queue.Queue") -> str:
    """生成配音成片，并缓存音轨与合成结果，避免重复 TTS 与合成。

    - 配音音轨（dub.m4a）只与 cues 相关，生成一次后复用。
    - 合成视频（video_dub.mp4）依赖烧录画面（sub_mode+样式），签名未变则复用。
    """
    dub_audio = os.path.join(job["dir"], "dub.m4a")
    if not os.path.exists(dub_audio):
        q.put({"stage": "tts", "done": 0, "total": len(job["cues"])})
        duration_ms = int((job["meta"].get("duration") or 0) * 1000) or None
        build_dub_track(
            job["cues"],
            dub_audio,
            total_duration_ms=duration_ms,
            progress=lambda done, total: q.put({"stage": "tts", "done": done, "total": total}),
        )

    sig = (sub_mode, dataclasses.astuple(style))
    out = os.path.join(job["dir"], "video_dub.mp4")
    cached = job.get("dub_video")
    if cached and cached["sig"] == sig and os.path.exists(out):
        return out
    q.put({"stage": "mux", "pct": 0})
    mux_dub_video(burned, dub_audio, out, progress=lambda pct: q.put({"stage": "mux", "pct": pct}))
    job["dub_video"] = {"sig": sig, "path": out}
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
            manifest = None if req.force else _load_manifest(job_dir)
            want_ai = bool(req.translate and req.ai_segment)
            video_exists = bool(
                manifest and os.path.exists(os.path.join(job_dir, manifest.get("video") or ""))
            )

            # 断句方式匹配：缓存 cues 可直接复用（含 AI 断句结果与翻译）。
            # 老 manifest 无 ai_segment 字段视为规则断句（False），行为不变。
            seg_match = bool(
                manifest
                and bool(manifest.get("ai_segment")) == want_ai
                and (not want_ai or manifest.get("ai_model", "") == (req.model or DEFAULT_MODEL))
            )

            if video_exists and seg_match:
                # 完全命中：同一 url 且断句方式一致，直接复用磁盘上的视频与 cues。
                cues = manifest.get("cues") or []
                meta = manifest.get("meta") or {}
                warning = None
                # 之前未翻译、本次要求翻译：只补翻译，不重新下载。
                if req.translate and not manifest.get("translated"):
                    q.put({"stage": "translate", "done": 0, "total": len(cues)})
                    try:
                        translate_cues(
                            cues,
                            model=req.model or DEFAULT_MODEL,
                            context=build_video_context(
                                meta.get("title"), meta.get("description")
                            ),
                            progress=lambda done, total: q.put(
                                {"stage": "translate", "done": done, "total": total}
                            ),
                        )
                        manifest["translated"] = True
                        manifest["cues"] = cues
                        _save_manifest(job_dir, manifest)
                    except ValueError as err:
                        warning = str(err)
                video_path = os.path.join(job_dir, manifest["video"])
                _JOBS[job_id] = {"dir": job_dir, "video": video_path, "cues": cues, "meta": meta}
                q.put(
                    {
                        "stage": "done",
                        "result": {
                            "job_id": job_id,
                            "title": meta.get("title", ""),
                            "lang": meta.get("lang", ""),
                            "kind": meta.get("kind", ""),
                            "duration": meta.get("duration", 0),
                            "video_url": _media_url(job_id, video_path),
                            "source_url": meta.get("source_url") or manifest.get("url", ""),
                            "thumbnail": meta.get("thumbnail", ""),
                            "cues": cues,
                            "warning": warning,
                            "cached": True,
                        },
                    }
                )
                return

            # 视频已存在但断句方式不匹配（如切换了 ai_segment 开关/模型）：
            # 复用视频跳过下载，重新拉字幕并断句。
            if video_exists:
                meta = manifest.get("meta") or {}
                video_path = os.path.join(job_dir, manifest["video"])
                duration = meta.get("duration") or 0
                thumbnail = meta.get("thumbnail") or ""
            else:
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
                video_path = downloaded.path
                duration = downloaded.duration
                thumbnail = downloaded.thumbnail

            q.put({"stage": "subtitle"})
            fetched = fetch_subtitle(req.url, req.lang)

            q.put({"stage": "segment"})
            prepared = prepare_timed_text_events(fetched.events)

            warning = None
            if req.translate and req.ai_segment:
                try:
                    cues = ai_format_subtitles(
                        prepared.flat_events,
                        fetched.lang,
                        model=req.model or DEFAULT_MODEL,
                        progress=lambda done, total: q.put(
                            {"stage": "segment", "done": done, "total": total}
                        ),
                    )
                except ValueError as err:
                    warning = str(err)  # 缺 API Key：整段降级为规则断句
                    cues = format_subtitles(prepared.flat_events, fetched.lang)
            else:
                cues = format_subtitles(prepared.flat_events, fetched.lang)

            if req.translate and warning is None:
                q.put({"stage": "translate", "done": 0, "total": len(cues)})
                try:
                    translate_cues(
                        cues,
                        model=req.model or DEFAULT_MODEL,
                        context=build_video_context(fetched.title, fetched.description),
                        progress=lambda done, total: q.put(
                            {"stage": "translate", "done": done, "total": total}
                        ),
                    )
                except ValueError as err:
                    warning = str(err)  # 缺 API Key：保留原文，前端提示

            meta = {
                "title": fetched.title,
                "lang": fetched.lang,
                "kind": fetched.kind,
                "duration": duration or fetched.duration,
                "description": (fetched.description or "")[:1000],
                "source_url": req.url,
                "thumbnail": thumbnail,
            }
            used_ai = bool(want_ai and warning is None)
            _JOBS[job_id] = {
                "dir": job_dir,
                "video": video_path,
                "cues": cues,
                "meta": meta,
            }
            _save_manifest(
                job_dir,
                {
                    "url": req.url,
                    "video": os.path.basename(video_path),
                    "cues": cues,
                    "translated": bool(req.translate and warning is None),
                    # 记录断句方式：缓存命中时校验 ai_segment 开关/模型一致，
                    # 避免切开关后复用错误断句方式的缓存结果。
                    "ai_segment": used_ai,
                    "ai_model": (req.model or DEFAULT_MODEL) if used_ai else "",
                    "meta": meta,
                },
            )

            q.put(
                {
                    "stage": "done",
                    "result": {
                        "job_id": job_id,
                        "title": fetched.title,
                        "lang": fetched.lang,
                        "kind": fetched.kind,
                        "duration": duration or fetched.duration,
                        "video_url": _media_url(job_id, video_path),
                        "source_url": req.url,
                        "thumbnail": thumbnail,
                        "cues": cues,
                        "warning": warning,
                        "cached": False,
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
    if req.mode not in ("original", "both"):
        raise HTTPException(status_code=400, detail="非法的 mode")

    q: "queue.Queue" = queue.Queue()

    def work() -> None:
        try:
            # 烧录一次即可：原声版直接是它，配音版只在其上换音轨。
            burned = _ensure_burned(job, req.sub_mode, style, q)
            videos: dict = {"original": _media_url(job_id, burned)}
            if req.mode == "both":
                out = _ensure_dub(job, burned, req.sub_mode, style, q)
                videos["dub"] = _media_url(job_id, out)
            q.put({"stage": "done", "result": {"videos": videos}})
        except Exception as err:  # noqa: BLE001
            q.put({"stage": "error", "detail": f"合成失败：{err}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    return StreamingResponse(_sse(q), media_type="text/event-stream")


@app.get("/api/cache")
def list_cache():
    """列出已缓存的任务及占用体积（供 UI 展示与清理）。"""
    items = []
    total_bytes = 0
    for name in sorted(os.listdir(WORKSPACE)):
        job_dir = os.path.join(WORKSPACE, name)
        if not os.path.isdir(job_dir):
            continue
        manifest = _load_manifest(job_dir)
        if not manifest:
            continue
        meta = manifest.get("meta") or {}
        size = _dir_size(job_dir)
        total_bytes += size
        items.append(
            {
                "job_id": name,
                "url": manifest.get("url", ""),
                "title": meta.get("title", ""),
                "translated": bool(manifest.get("translated")),
                "bytes": size,
            }
        )
    return {"items": items, "total_bytes": total_bytes}


@app.delete("/api/cache")
def clear_cache():
    """清空所有缓存任务。"""
    cleared = 0
    for name in list(os.listdir(WORKSPACE)):
        job_dir = os.path.join(WORKSPACE, name)
        if os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
            cleared += 1
    _JOBS.clear()
    return {"cleared": cleared}


@app.delete("/api/cache/{job_id}")
def delete_cache(job_id: str):
    """删除单个 url 对应的缓存。"""
    job_dir = _safe_job_dir(job_id)
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)
    _JOBS.pop(job_id, None)
    return {"ok": True}


app.mount("/media", StaticFiles(directory=WORKSPACE), name="media")
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
