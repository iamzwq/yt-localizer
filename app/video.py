"""阶段 1.5：用 yt-dlp 下载视频文件（含原声），供烧录/配音使用。"""

import os
from dataclasses import dataclass
from typing import Callable, Optional

from .ytdlp_opts import base_ydl_opts


@dataclass
class DownloadedVideo:
    path: str
    video_id: str
    title: str
    duration: float
    thumbnail: str = ""


def download_video(
    url: str,
    output: Optional[str] = None,
    output_dir: str = ".",
    fmt: str = "bestvideo*+bestaudio/best",
    merge_format: str = "mp4",
    progress_hook: Optional[Callable[[dict], None]] = None,
) -> DownloadedVideo:
    """下载并合并为单个视频文件，返回最终路径与元信息。

    ``progress_hook`` 传给 yt-dlp，可接收下载进度回调。
    """
    import yt_dlp  # 延迟导入，便于无 yt-dlp 环境下导入本模块

    outtmpl = output or os.path.join(output_dir, "%(id)s.%(ext)s")
    opts = {
        **base_ydl_opts(),
        "format": fmt,
        "merge_output_format": merge_format,
        "outtmpl": outtmpl,
    }
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)

    # 合并后扩展名可能变为 merge_format。
    base, _ = os.path.splitext(path)
    merged = f"{base}.{merge_format}"
    final_path = merged if os.path.exists(merged) else path

    return DownloadedVideo(
        path=final_path,
        video_id=info.get("id") or "",
        title=info.get("title") or "",
        duration=info.get("duration") or 0,
        thumbnail=info.get("thumbnail") or "",
    )
