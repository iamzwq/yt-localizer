"""字幕处理地基：清洗展平 + 规则断句。

典型用法::

    from app.subtitle import prepare_timed_text_events, format_subtitles

    prepared = prepare_timed_text_events(json3["events"])
    cues = format_subtitles(prepared.flat_events, from_lang)
"""

from .clean import clean_timed_text
from .prepare import FlatEvent, PreparedEvents, prepare_timed_text_events
from .ai_segment import ai_format_subtitles
from .segment import (
    Cue,
    format_subtitles,
    is_quality_poor,
    process_subtitles,
)
from .text_classification import is_non_speech_segment

__all__ = [
    "clean_timed_text",
    "is_non_speech_segment",
    "prepare_timed_text_events",
    "PreparedEvents",
    "FlatEvent",
    "format_subtitles",
    "ai_format_subtitles",
    "process_subtitles",
    "is_quality_poor",
    "Cue",
]
