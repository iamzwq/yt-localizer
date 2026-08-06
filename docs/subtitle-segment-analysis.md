# 字幕断句功能实现分析（app/subtitle/segment.py）

## 概述

`segment.py` 是从 kiss-translator 插件的 `processSubtitles` / `formatSubtitles` / `isQualityPoor` 精确移植的"规则断句"模块，是当前唯一的断句策略。

- 输入：词级时间戳事件 `FlatEvent`，结构为 `{"text": str, "start": int|float, "end": int|float}`
- 输出：句子级字幕 `Cue`，结构为 `{"start": int|float, "end": int|float, "text": str}`
- 主入口：`format_subtitles(flat_events, lang, long_sentence_threshold=100)`，按语言类型分流到两套完全不同的断句策略。

调用方：[cli.py](../app/cli.py)、[server.py](../app/server.py)、[app/subtitle/**init**.py](../app/subtitle/__init__.py) 均通过 `format_subtitles(prepared.flat_events, fetched.lang)` 使用该模块。

## 1. 空格语言（英/欧）：`process_subtitles` 状态机

逐词遍历，判断"当前缓冲区是否应该在这个新词之前截断"，触发截断的条件（任一为真即 `flush`）：

| 条件                     | 说明                                                                                                                             |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `is_end_of_sentence`     | 上一个词以 `.?!…])` 结尾（`_END_OF_SENTENCE_RE`）                                                                                |
| `is_timeout`             | 与上一词间隔 > `timeout`（默认 1000ms），即静音过长                                                                              |
| `is_duration_exceeded`   | 缓冲区总时长 ≥ `max_duration_ms`（默认 10000ms）                                                                                 |
| `is_word_limit_exceeded` | 上一词以逗号结尾（或开启 `use_pause`）且词数 ≥ `max_words`（默认 15）                                                            |
| `starts_with_sign`       | 新词以 `[`、`(`、`♪` 开头（字幕特效/音乐标记独立成句）                                                                           |
| `starts_with_pause_word` | `use_pause=True` 时，新词是 `_PAUSE_WORDS` 词库中的逻辑连词（and/but/so/however 等），且缓冲区已有 ≥2 个词，用于长句二次语义拆分 |

## 2. 无空格语言（中/日/韩/泰等）：字符级拼接

语言判定：`lang` 以 `_NO_SPACE_LANGUAGES = ("zh", "ja", "ko", "th", "lo", "km", "my")` 中任一前缀开头。

- 先用 `is_quality_poor(flat_events, 5, 0.5)` 检测：若源字幕中"长行"（长度 > 5 字符）占比超过 50%，说明原始切分质量差，直接放弃断句、原样返回，避免规则断句在脏数据上产生更差结果。
- 否则按字符拼接，触发结束（`subtitles.append` 并重置 `current_line`）的条件：
  - 静音间隔 > `pause_threshold_ms`（1000ms）→ 先把已有缓冲区收尾（避免跨越长停顿合并）
  - 当前片段命中 `_CJK_END_OF_SENTENCE_RE`（句末标点 `。！？.!?…`，允许跟随收尾引号/括号）
  - 累计长度 ≥ `max_length`（30 字符）

## 3. `format_subtitles` 的二次拆分逻辑

仅对**空格语言**分支生效：先跑一遍 `process_subtitles` 得到句子，再对超过 `long_sentence_threshold`（默认 100，函数签名默认 100，注释提及原插件默认 120）的长句：

1. 取回该句子时间范围内的原始词级事件（`sub["start"] <= e["start"] < sub["end"]`）
2. 若词数 > 1，用 `use_pause=True` 重新跑一遍状态机做更激进的拆分（依赖逻辑连词）
3. 否则（只有 1 个词，无法再拆）原样保留

## 观察到的问题点

1. **CJK 分支没有长句二次拆分**：`format_subtitles` 中 CJK 分支在函数中部直接 `return subtitles`，不会流经末尾的 `long_sentence_threshold` 二次拆分逻辑，该逻辑只对空格语言生效。
2. **`_word_count` 仅按空白切分**：对中英混排文本（如英文字幕里夹中文专有名词）没有特殊处理，但因为只用于空格语言分支，影响有限。
3. **`is_quality_poor` 阈值不一致**：函数默认参数为 `length_threshold=200, percentage_threshold=0.1`，但 `format_subtitles` 内实际调用时硬编码传入 `(5, 0.5)`，与默认值差异较大，实际生效的是调用处传入值，容易造成阅读时的误解。
4. 常量 `_PAUSE_WORDS` 及各类阈值均从原 JS 插件直接移植，属经验值，暂无进一步的本地化调优记录。

## 测试覆盖情况（tests/test_segment.py）

- 句末标点断句（`test_process_subtitles_breaks_on_sentence_punctuation`）
- 长静音超时断句（`test_process_subtitles_breaks_on_long_silence`）
- 特效符号开头独立成句（`test_process_subtitles_starts_with_sign_breaks`）
- CJK 标点/长度断句（`test_format_subtitles_cjk_breaks_on_length_and_punct`）
- 长句二次拆分（`test_format_subtitles_splits_long_sentence`）
- 质量检测（`test_is_quality_poor`）

整体测试覆盖了主要分支，逻辑清晰，仅靠正则 + 简单状态机实现，无外部依赖，性能和可维护性较好。
