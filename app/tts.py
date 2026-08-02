"""阶段 7：edge-TTS 中文配音 + 时间轴对齐 + 视频 B 合成。

- ``plan_dub_timeline``：纯函数，根据每句音频原始时长决定放置位置与变速，
  超出可用时长时按上限加速，仍超则轻微顺延（drift）。
- 合成/组轨/换音轨为 ffmpeg + edge-tts 的 IO 封装。
"""

import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional

Cue = Dict[str, Any]

DEFAULT_VOICE = "zh-CN-YunxiNeural"
DEFAULT_SAMPLE_RATE = 44100


class DubError(Exception):
    """配音音轨构建失败。"""


def plan_dub_timeline(
    cues: List[Cue],
    durations_ms: List[float],
    max_speedup: float = 1.5,
) -> List[Dict[str, Any]]:
    """规划每句配音的放置时间与变速倍率。

    返回每句 ``{index, at, speed, play_duration, source_duration}``：
    - ``at``：在最终音轨上的起始毫秒（含顺延后的实际位置）。
    - ``speed``：atempo 倍率（1.0 表示不变速）。
    - ``play_duration``：变速后时长（毫秒）。
    """
    segments: List[Dict[str, Any]] = []
    cursor = 0.0
    count = len(cues)
    for i, cue in enumerate(cues):
        start = cue["start"]
        source = max(0.0, float(durations_ms[i]))
        next_start = cues[i + 1]["start"] if i + 1 < count else start + source
        slot = max(0.0, next_start - start)

        speed = 1.0
        play_duration = source
        if slot > 0 and source > slot:
            speed = min(max_speedup, source / slot)
            play_duration = source / speed

        at = max(cursor, float(start))  # 上一句超时则顺延
        segments.append(
            {
                "index": i,
                "at": int(round(at)),
                "speed": round(speed, 4),
                "play_duration": int(round(play_duration)),
                "source_duration": int(round(source)),
            }
        )
        cursor = at + play_duration

    return segments


def _cue_dub_text(cue: Cue) -> str:
    return str(cue.get("translation") or cue.get("text") or "").strip()


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
    max_speedup: float = 1.5,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    total_duration_ms: Optional[int] = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    workdir: Optional[str] = None,
    synth: Optional[Callable[[str, str], None]] = None,
    probe: Optional[Callable[[str], int]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """合成整条配音音轨并对齐到视频时间轴。

    ``synth(text, out)`` 与 ``probe(path)`` 可注入，便于测试或替换后端。
    ``progress(done, total)`` 每合成一句后回调，用于上报配音进度。
    """
    if not cues:
        raise DubError("没有可配音的字幕")

    synth = synth or (lambda text, out: synthesize_cue(text, out, voice=voice, rate=rate))
    probe = probe or (lambda path: probe_duration_ms(path, ffprobe))

    tmp = workdir or tempfile.mkdtemp(prefix="dub_")
    clip_paths: List[Optional[str]] = []
    durations: List[float] = []
    total = len(cues)
    for i, cue in enumerate(cues):
        text = _cue_dub_text(cue)
        if text:
            clip = os.path.join(tmp, f"clip_{i:05d}.mp3")
            synth(text, clip)
            clip_paths.append(clip)
            durations.append(probe(clip))
        else:
            clip_paths.append(None)
            durations.append(0)
        if progress:
            progress(i + 1, total)

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
        cmd += ["-filter_complex", filter_complex, "-map", "[out]", *codec, output_path]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DubError(proc.stderr[-2000:])
    return output_path


def mux_dub_video(
    video_path: str,
    audio_path: str,
    output_path: str,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> str:
    """用配音音轨替换视频音轨；若配音比视频长，克隆最后一帧补足画面。"""
    abs_video = os.path.abspath(video_path)
    abs_audio = os.path.abspath(audio_path)
    pad_ms = max(0, probe_duration_ms(abs_audio, ffprobe) - probe_duration_ms(abs_video, ffprobe))

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
    cmd += ["-c:a", "aac", "-b:a", "192k", os.path.abspath(output_path)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DubError(proc.stderr[-2000:])
    return output_path
