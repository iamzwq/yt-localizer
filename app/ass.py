"""cues → ASS 字幕（带样式：字体、字号、文字色、背景框）。

用于 ffmpeg 烧录。样式参数与前端预览同源，保证所见即所得。
字体默认 “霞鹜文楷等宽 (LXGW WenKai Mono)”，libass 找不到时自动降级系统默认。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .srt import MODE_BILINGUAL, _lines_for_mode

Cue = Dict[str, Any]


@dataclass
class SubtitleStyle:
    font_name: str = "LXGW WenKai Mono"
    font_size: int = 42  # 相对 play_res_y 的字号，随视频分辨率等比缩放
    text_color: str = "#FFFFFF"
    bg_color: str = "#000000"
    bg_opacity: float = 0.6  # 背景框不透明度 0~1
    outline: float = 4.0  # BorderStyle=3 下即背景框的内边距
    margin_v: int = 40
    play_res_y: int = 720
    bold: bool = False


def _ass_time(ms: float) -> str:
    """毫秒 → ASS 时间 ``H:MM:SS.cc``（厘秒）。"""
    cs = max(0, int(round(ms / 10)))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    seconds, cs = divmod(cs, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def _ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """``#RRGGBB`` + 不透明度 → ASS ``&HAABBGGRR``（AA=00 不透明）。"""
    h = str(hex_color).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = max(0, min(255, int(round((1 - opacity) * 255))))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def _escape_text(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .strip()
    )


def _dialogue_text(cue: Cue, mode: str) -> str:
    lines = [_escape_text(ln) for ln in _lines_for_mode(cue, mode) if str(ln).strip()]
    return "\\N".join(lines)


def build_ass(
    cues: List[Cue],
    style: Optional[SubtitleStyle] = None,
    mode: str = MODE_BILINGUAL,
) -> str:
    style = style or SubtitleStyle()
    play_res_y = style.play_res_y
    play_res_x = int(round(play_res_y * 16 / 9))

    primary = _ass_color(style.text_color, 1.0)
    back = _ass_color(style.bg_color, style.bg_opacity)
    bold = -1 if style.bold else 0

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        # BorderStyle=3 用 BackColour 作为文字背景框。
        (
            f"Style: Default,{style.font_name},{style.font_size},{primary},"
            f"&H000000FF,{back},{back},{bold},0,0,0,100,100,0,0,3,"
            f"{style.outline},0,2,40,40,{style.margin_v},1"
        ),
        "",
        "[Events]",
        (
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text"
        ),
    ]

    for cue in cues:
        text = _dialogue_text(cue, mode)
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(cue['end'])},"
            f"Default,,0,0,0,,{text}"
        )

    return "\n".join(lines) + "\n"
