"""edge-TTS 中文配音 + 时间轴对齐 + 视频 B 合成。

- ``plan_dub_timeline``：纯函数，每句音频独立锚定在自身字幕时间窗（start~end）内，
  原声时长超出窗口时按 ``atempo`` 倍率加速塞入，不做跨句顺延，避免误差累积。
- 合成/组轨/换音轨为 ffmpeg + edge-tts 的 IO 封装。
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging import getLogger
from typing import Any, Callable, Dict, List, Optional

Cue = Dict[str, Any]

logger = getLogger(__name__)

DEFAULT_VOICE = "zh-CN-YunyangNeural" # edge-tts --list-voices | grep "zh-"
DEFAULT_SAMPLE_RATE = 44100

# 含字母/数字/CJK/假名/谙文才可发音；纯标点符号（如 ♪、…、—）会让 edge-tts 返回空音频。
_SPEAKABLE_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]")


class DubError(Exception):
    """配音音轨构建失败。"""


def _run_ffmpeg_with_progress(
    cmd: List[str],
    total_duration_ms: Optional[int],
    progress: Callable[[int], None],
) -> None:
    """运行 ffmpeg 并解析 ``-progress`` 输出的 ``out_time_us`` 推算百分比。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        raise DubError(stderr[-2000:])
    progress(100)


def plan_dub_timeline(
    cues: List[Cue],
    durations_ms: List[float],
    max_speedup: float = 1.6,
) -> List[Dict[str, Any]]:
    """规划每句配音的放置时间与变速倍率（不做顺延，每句独立锚定自身时间窗）。

    返回每句 ``{index, at, speed, play_duration, source_duration}``：
    - ``at``：恒等于该句字幕自身的 ``start``，不依赖其他句是否超时——
      因此不存在“一句超时、后面全部跟着晚”的顺延累积问题。
    - ``speed``：atempo 倍率（1.0 表示不变速），按“该句原声时长 / 该句字幕自身
      显示时长(end-start)”计算，超过 ``max_speedup`` 时按上限截断。
    - ``play_duration``：变速后时长（毫秒）；若语速已到上限仍装不下，允许这一句
      音频尾部超出自己的字幕窗口，但绝不影响下一句的起播时间。
    """
    segments: List[Dict[str, Any]] = []
    for i, cue in enumerate(cues):
        start = float(cue["start"])
        window = max(0.0, float(cue.get("end", start)) - start)
        source = max(0.0, float(durations_ms[i]))

        speed = 1.0
        play_duration = source
        if window > 0 and source > window:
            speed = min(max_speedup, source / window)
            play_duration = source / speed

        segments.append(
            {
                "index": i,
                "at": int(round(start)),
                "speed": round(speed, 4),
                "play_duration": int(round(play_duration)),
                "source_duration": int(round(source)),
            }
        )

    return segments


def _cue_dub_text(cue: Cue) -> str:
    text = str(cue.get("translation") or cue.get("text") or "").strip()
    return text if _SPEAKABLE_RE.search(text) else ""


def synthesize_cue(
    text: str,
    out_path: str,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
) -> str:
    """用 edge-tts 合成单句中文语音到 out_path。"""
    import asyncio

    import edge_tts

    async def _run() -> None:
        await edge_tts.Communicate(text, voice, rate=rate).save(out_path)

    asyncio.run(_run())
    return out_path


def probe_duration_ms(path: str, ffprobe: str = "ffprobe") -> int:
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return int(round(float(proc.stdout.strip()) * 1000))
    except (ValueError, TypeError):
        return 0


def build_dub_track(
    cues: List[Cue],
    output_path: str,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    max_speedup: float = 1.6,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    total_duration_ms: Optional[int] = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    workdir: Optional[str] = None,
    synth: Optional[Callable[[str, str], None]] = None,
    probe: Optional[Callable[[str], int]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    concurrency: int = 4,
    retries: int = 2,
) -> str:
    """合成整条配音音轨并对齐到视频时间轴。

    ``synth(text, out)`` 与 ``probe(path)`` 可注入，便于测试或替换后端。
    ``progress(done, total)`` 每合成一句后回调，用于上报配音进度。
    ``retries``：单句合成异常或返回空音频时的重试次数（指数退避），耗尽后降级为静音。
    未显式传入 ``workdir`` 时，分片音频写入自建临时目录，函数结束前会自动清理。
    """
    if not cues:
        raise DubError("没有可配音的字幕")

    synth = synth or (lambda text, out: synthesize_cue(text, out, voice=voice, rate=rate))
    probe = probe or (lambda path: probe_duration_ms(path, ffprobe))

    owns_tmp = workdir is None
    tmp = workdir or tempfile.mkdtemp(prefix="dub_")
    total = len(cues)

    def _one(i: int) -> tuple:
        text = _cue_dub_text(cues[i])
        if not text:
            return i, None, 0
        clip = os.path.join(tmp, f"clip_{i:05d}.mp3")
        last_err: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                synth(text, clip)
                dur = probe(clip)
                if dur > 0:
                    return i, clip, dur
                last_err = None  # 合成未报错但时长为 0，视为空音频仍需重试
            except Exception as err:  # noqa: BLE001 - 重试耗尽后降级为静音，不中断整体
                last_err = err
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
        logger.warning("第 %d 句配音合成失败，降级为静音：%s", i, last_err or "返回空音频")
        return i, None, 0

    clip_paths: List[Optional[str]] = [None] * total
    durations: List[float] = [0] * total
    done = 0
    try:
        # edge-tts 是网络 IO，多句并发可大幅缩短合成耗时；
        # 按完成顺序而非提交顺序回报进度，避免长句阻塞后面已完成句的进度更新。
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futures = [ex.submit(_one, i) for i in range(total)]
            for future in as_completed(futures):
                i, clip, dur = future.result()
                clip_paths[i] = clip
                durations[i] = dur
                done += 1
                if progress:
                    progress(done, total)

        segments = plan_dub_timeline(cues, durations, max_speedup=max_speedup)

        inputs: List[str] = []
        filter_parts: List[str] = []
        mix_labels: List[str] = []
        for seg in segments:
            clip = clip_paths[seg["index"]]
            if not clip or seg["play_duration"] <= 0:
                continue
            idx = len(inputs)
            inputs.append(clip)
            chain = []
            if abs(seg["speed"] - 1.0) > 1e-3:
                chain.append(f"atempo={seg['speed']:.4f}")
            chain.append(f"adelay={seg['at']}:all=1")
            label = f"a{idx}"
            filter_parts.append(f"[{idx}:a]{','.join(chain)}[{label}]")
            mix_labels.append(f"[{label}]")

        # 音轨长度取“视频时长”与“配音实际结束”的较大值，避免末段配音被截断。
        dub_end = max((seg["at"] + seg["play_duration"] for seg in segments), default=0)
        total_duration_ms = max(total_duration_ms or 0, dub_end)
        total_sec = max(0.001, total_duration_ms / 1000)

        cmd = [ffmpeg, "-y"]
        for clip in inputs:
            cmd += ["-i", clip]

        base_index = len(inputs)
        cmd += ["-f", "lavfi", "-t", f"{total_sec:.3f}", "-i", f"anullsrc=r={sample_rate}:cl=mono"]

        codec = ["-c:a", "pcm_s16le"] if output_path.lower().endswith(".wav") else ["-c:a", "aac", "-b:a", "192k"]

        filter_script_path = None
        if not mix_labels:
            # 全部为空文本：直接产出等长静音轨。
            cmd += [*codec, output_path]
        else:
            base_label = f"[{base_index}:a]"
            mix_inputs = base_label + "".join(mix_labels)
            filter_complex = (
                ";".join(filter_parts)
                + f";{mix_inputs}amix=inputs={len(mix_labels) + 1}:normalize=0:duration=first[out]"
            )
            # cues 很多时内联 filter_complex 会很长，写入脚本文件避免命令行长度限制。
            filter_script_fd, filter_script_path = tempfile.mkstemp(suffix=".txt", dir=tmp)
            with os.fdopen(filter_script_fd, "w", encoding="utf-8") as f:
                f.write(filter_complex)
            cmd += ["-filter_complex_script", filter_script_path, "-map", "[out]", *codec, output_path]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            if filter_script_path:
                try:
                    os.remove(filter_script_path)
                except OSError:
                    pass
        if proc.returncode != 0:
            raise DubError(proc.stderr[-2000:])
        return output_path
    finally:
        if owns_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def mux_dub_video(
    video_path: str,
    audio_path: str,
    output_path: str,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    progress: Optional[Callable[[int], None]] = None,
) -> str:
    """用配音音轨替换视频音轨；若配音比视频长，克隆最后一帧补足画面。

    ``progress(pct)`` 非空时用 ffmpeg ``-progress`` 流式上报 0~100 百分比。
    """
    abs_video = os.path.abspath(video_path)
    abs_audio = os.path.abspath(audio_path)
    video_ms = probe_duration_ms(abs_video, ffprobe)
    audio_ms = probe_duration_ms(abs_audio, ffprobe)
    pad_ms = max(0, audio_ms - video_ms)
    total_ms = max(video_ms, audio_ms) or None

    cmd = [ffmpeg, "-y", "-i", abs_video, "-i", abs_audio, "-map", "0:v:0", "-map", "1:a:0"]
    if pad_ms > 40:
        # 配音更长：冻结末帧延长视频（需重编码），保证末段配音读完。
        cmd += [
            "-vf",
            f"tpad=stop_mode=clone:stop_duration={pad_ms / 1000:.3f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        cmd += ["-c:v", "copy"]
    if progress is not None:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += ["-c:a", "aac", "-b:a", "192k", os.path.abspath(output_path)]

    if progress is None:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise DubError(proc.stderr[-2000:])
    else:
        _run_ffmpeg_with_progress(cmd, total_ms, progress)
    return output_path
