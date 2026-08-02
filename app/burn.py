"""阶段 8：用 ffmpeg 把 ASS 字幕烧录进视频。

生成视频 A（中文字幕 + 原声）；配音版在 TTS 阶段接入后复用同一烧录画面。
"""

import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional

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
    total_duration_ms: Optional[int] = None,
    progress: Optional[Callable[[int], None]] = None,
) -> str:
    """把 cues 烧录进 video_path，保留原音轨，输出到 output_path。

    ``progress(pct)`` 非空时用 ffmpeg ``-progress`` 流式上报 0~100 百分比。
    """
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

        cmd = [ffmpeg, "-y", "-i", abs_video, "-vf", vf, "-c:a", "copy"]
        if progress is not None:
            cmd += ["-progress", "pipe:1", "-nostats"]
        cmd += [abs_output]

        if progress is None:
            proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
            if proc.returncode != 0:
                raise FfmpegError(proc.stderr[-2000:])
        else:
            _run_with_progress(cmd, work_dir, total_duration_ms, progress)
    finally:
        try:
            os.remove(ass_path)
        except OSError:
            pass

    return output_path


def _run_with_progress(
    cmd: List[str],
    work_dir: str,
    total_duration_ms: Optional[int],
    progress: Callable[[int], None],
) -> None:
    """运行 ffmpeg 并解析 ``-progress`` 输出的 ``out_time_us`` 推算百分比。"""
    proc = subprocess.Popen(
        cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    last = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and total_duration_ms:
            try:
                done_ms = int(line.split("=", 1)[1]) / 1000
            except ValueError:
                continue
            pct = min(99, int(done_ms * 100 / total_duration_ms))
            if pct > last:
                last = pct
                progress(pct)
    proc.wait()
    stderr = proc.stderr.read() if proc.stderr else ""
    if proc.returncode != 0:
        raise FfmpegError(stderr[-2000:])
    progress(100)
