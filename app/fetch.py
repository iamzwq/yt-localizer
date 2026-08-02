"""阶段 1：用 yt-dlp 取视频信息与 json3 词级字幕。

``select_caption_track`` 为纯函数，可脱离网络单测；``fetch_subtitle`` 负责
真正的 yt-dlp 抽取与 json3 下载。
"""

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ytdlp_opts import USER_AGENT, base_ydl_opts


class SubtitleNotFoundError(Exception):
    """视频没有可用的 json3 字幕轨。"""


@dataclass
class CaptionTrack:
    lang: str
    url: str
    kind: str  # "manual" 人工字幕 / "auto" 自动生成(ASR)


@dataclass
class FetchedSubtitle:
    events: List[Dict[str, Any]]
    lang: str
    kind: str
    title: str = ""
    description: str = ""
    duration: float = 0
    webpage_url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _find_json3_url(subs: Dict[str, Any], lang: str) -> Optional[str]:
    for fmt in subs.get(lang) or []:
        if fmt.get("ext") == "json3" and fmt.get("url"):
            return fmt["url"]
    return None


def _match_lang(available_langs: List[str], wanted: Optional[str]) -> Optional[str]:
    """精确匹配优先，其次按语言主类（如 en 匹配 en-US）匹配。"""
    if not wanted:
        return None
    if wanted in available_langs:
        return wanted
    base = wanted.split("-")[0]
    for lang in available_langs:
        if lang == base or lang.split("-")[0] == base:
            return lang
    return None


def select_caption_track(
    info: Dict[str, Any], prefer_lang: Optional[str] = None
) -> Optional[CaptionTrack]:
    """从 yt-dlp info 中挑选最合适的 json3 字幕轨。

    优先级：人工字幕 > 自动字幕；语言：prefer_lang > 视频原语言 > 任意可用。
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    original = info.get("language")

    for source, kind in ((manual, "manual"), (auto, "auto")):
        langs = list(source.keys())
        for wanted in (prefer_lang, original):
            matched = _match_lang(langs, wanted)
            if matched:
                url = _find_json3_url(source, matched)
                if url:
                    return CaptionTrack(matched, url, kind)

    # 兜底：任取一条带 json3 的轨道，人工优先。
    for source, kind in ((manual, "manual"), (auto, "auto")):
        for lang, formats in source.items():
            for fmt in formats:
                if fmt.get("ext") == "json3" and fmt.get("url"):
                    return CaptionTrack(lang, fmt["url"], kind)

    return None


def _http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_subtitle(url: str, prefer_lang: Optional[str] = None) -> FetchedSubtitle:
    """抽取视频信息并下载选中轨道的 json3 事件。"""
    import yt_dlp  # 延迟导入，便于纯函数单测无需安装 yt-dlp

    ydl_opts = {
        **base_ydl_opts(),
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "json3",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    track = select_caption_track(info, prefer_lang)
    if not track:
        raise SubtitleNotFoundError(f"未找到可用的 json3 字幕轨: {url}")

    data = json.loads(_http_get(track.url))
    events = data.get("events") or []

    return FetchedSubtitle(
        events=events,
        lang=track.lang,
        kind=track.kind,
        title=info.get("title") or "",
        description=info.get("description") or "",
        duration=info.get("duration") or 0,
        webpage_url=info.get("webpage_url") or url,
    )
