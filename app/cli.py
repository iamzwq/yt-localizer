"""命令行入口：下载 YouTube 字幕并导出 SRT（原文/译文/双语）。

用法::

    # 原文（无需 API Key）
    python -m app.cli "https://youtu.be/xxxx" -o out.srt

    # 中文译文 / 双语（需 DEEPSEEK_API_KEY 环境变量或 --api-key）
    python -m app.cli "https://youtu.be/xxxx" -o out.zh.srt --mode translated
    python -m app.cli "https://youtu.be/xxxx" -o out.bi.srt --mode bilingual
"""

import argparse
import sys

from .fetch import SubtitleNotFoundError, fetch_subtitle
from .srt import MODE_BILINGUAL, MODE_ORIGINAL, MODE_TRANSLATED, build_srt
from .subtitle import ai_format_subtitles, format_subtitles, prepare_timed_text_events
from .translate import DEFAULT_MODEL, build_video_context, translate_cues


def run(
    url: str,
    output: str,
    prefer_lang=None,
    mode: str = MODE_ORIGINAL,
    api_key=None,
    model: str = DEFAULT_MODEL,
    ai_segment: bool = False,
) -> int:
    try:
        fetched = fetch_subtitle(url, prefer_lang)
    except SubtitleNotFoundError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    prepared = prepare_timed_text_events(fetched.events)
    wants_translation = mode in (MODE_TRANSLATED, MODE_BILINGUAL)

    if wants_translation and ai_segment:
        try:
            cues = ai_format_subtitles(
                prepared.flat_events,
                fetched.lang,
                api_key=api_key,
                model=model,
                progress=lambda done, total: print(
                    f"AI 断句 {done}/{total}", file=sys.stderr
                ),
            )
        except ValueError as err:
            print(f"警告：{err}，AI 断句降级为规则断句", file=sys.stderr)
            cues = format_subtitles(prepared.flat_events, fetched.lang)
    else:
        cues = format_subtitles(prepared.flat_events, fetched.lang)

    if wants_translation:
        try:
            translate_cues(
                cues,
                api_key=api_key,
                model=model,
                context=build_video_context(fetched.title, fetched.description),
            )
        except ValueError as err:
            print(f"错误：{err}", file=sys.stderr)
            return 3

    srt_text = build_srt(cues, mode=mode)
    with open(output, "w", encoding="utf-8") as f:
        f.write(srt_text)

    print(
        f"已写入 {len(cues)} 条字幕 → {output} "
        f"(模式={mode}, 语言={fetched.lang}, 来源={fetched.kind}, 标题={fetched.title})"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="下载 YouTube 字幕并导出 SRT")
    parser.add_argument("url", help="YouTube 视频链接")
    parser.add_argument("-o", "--output", default="output.srt", help="输出 SRT 路径")
    parser.add_argument("--lang", default=None, help="优先字幕语言，默认视频原语言")
    parser.add_argument(
        "--mode",
        choices=[MODE_ORIGINAL, MODE_TRANSLATED, MODE_BILINGUAL],
        default=MODE_ORIGINAL,
        help="original 原文 / translated 中文译文 / bilingual 双语",
    )
    parser.add_argument("--api-key", default=None, help="deepseek API Key（默认取环境变量）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="deepseek 模型名")
    parser.add_argument(
        "--ai-segment",
        action="store_true",
        help="用 AI 断句+翻译合并替代规则断句（仅 translated/bilingual 模式下生效，失败自动降级）",
    )
    args = parser.parse_args(argv)
    return run(
        args.url, args.output, args.lang, args.mode, args.api_key, args.model, args.ai_segment
    )


if __name__ == "__main__":
    raise SystemExit(main())
