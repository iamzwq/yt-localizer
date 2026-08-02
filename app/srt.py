"""阶段 5：cues → SRT 字幕文件。

支持三种产物：原文 / 译文 / 双语。时间戳采用 SRT 规范（逗号分隔毫秒）。
"""

from typing import Any, Dict, List

Cue = Dict[str, Any]

MODE_ORIGINAL = "original"
MODE_TRANSLATED = "translated"
MODE_BILINGUAL = "bilingual"


def format_timestamp(ms: float) -> str:
    """毫秒 → ``HH:MM:SS,mmm``。"""
    ms = max(0, int(round(ms)))
    hours, ms = divmod(ms, 3600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, millis = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _lines_for_mode(cue: Cue, mode: str) -> List[str]:
    text = str(cue.get("text") or "").strip()
    translation = str(cue.get("translation") or "").strip()

    if mode == MODE_TRANSLATED:
        return [translation or text]
    if mode == MODE_BILINGUAL:
        return [text, translation] if translation else [text]
    return [text]


def build_srt(cues: List[Cue], mode: str = MODE_ORIGINAL) -> str:
    """把 cues 渲染为 SRT 文本。"""
    blocks = []
    index = 1
    for cue in cues:
        lines = [ln for ln in _lines_for_mode(cue, mode) if ln]
        if not lines:
            continue
        timestamp = f"{format_timestamp(cue['start'])} --> {format_timestamp(cue['end'])}"
        blocks.append(f"{index}\n{timestamp}\n" + "\n".join(lines))
        index += 1

    return ("\n\n".join(blocks) + "\n") if blocks else ""
