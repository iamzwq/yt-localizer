"""阶段 8：用 ffmpeg 把 ASS 字幕烧录进视频。

生成视频 A（中文字幕 + 原声）；配音版在 TTS 阶段接入后复用同一烧录画面。
"""

import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from .ass import SubtitleStyle, build_ass
from .srt import MODE_BILINGUAL

Cue = Dict[str, Any]


class FfmpegError(Exception):
    """ffmpeg 执行失败。"""


def escape_ass_path_for_filter(path: str) -> str:
    """转义 ffmpeg 滤镜里的 ass 文件路径（Windows 盘符冒号需转义）。"""
    return path.replace("\\", "/").replace(":", "\\:")


def burn_subtitles(
    video_path: str,
    cues: List[Cue],
    output_path: str,
    *,
    style: Optional[SubtitleStyle] = None,
    mode: str = MODE_BILINGUAL,
    fonts_dir: Optional[str] = None,
    ffmpeg: str = "ffmpeg",
) -> str:
    """把 cues 烧录进 video_path，保留原音轨，输出到 output_path。"""
    style = style or SubtitleStyle()
    ass_text = build_ass(cues, style, mode)

    # 把 ass 写到输出目录并用相对文件名 + cwd，绕开 Windows 盘符冒号在
    # ffmpeg 滤镜里的转义问题（绝对路径的 "C:" 会被误当成第二个滤镜参数）。
    abs_video = os.path.abspath(video_path)
    abs_output = os.path.abspath(output_path)
    work_dir = os.path.dirname(abs_output) or "."
    fd, ass_path = tempfile.mkstemp(suffix=".ass", dir=work_dir)
    os.close(fd)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_text)

    try:
        vf = f"ass={os.path.basename(ass_path)}"
        if fonts_dir:
            vf += f":fontsdir={escape_ass_path_for_filter(os.path.abspath(fonts_dir))}"

        cmd = [
            ffmpeg,
            "-y",
            "-i",
            abs_video,
            "-vf",
            vf,
            "-c:a",
            "copy",
            abs_output,
        ]
        proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            raise FfmpegError(proc.stderr[-2000:])
    finally:
        try:
            os.remove(ass_path)
        except OSError:
            pass

    return output_path
