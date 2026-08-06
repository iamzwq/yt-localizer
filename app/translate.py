"""阶段 4：deepseek 批量翻译（逐条对齐 + 失败重试）。

设计：断句已完成，这里只对 cues 逐条翻译成中文，保证 N 进 N 出。
- 纯函数（分批、构造消息、解析响应）可脱离网络单测。
- ``call_llm`` 可注入，便于测试；默认走 deepseek OpenAI 兼容接口。
- 单批最多尝试 ``max_retries`` 次；全败则该批降级保留原文。
"""

import json
import os
import re
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from . import config  # noqa: F401  导入即加载 .env

Cue = Dict[str, Any]
CallLLM = Callable[[List[Dict[str, str]]], str]

# 模型与接口地址支持 .env 配置（DEEPSEEK_MODEL / DEEPSEEK_BASE_URL）。
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions"
)

_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z0-9]*\n")
_FENCE_CLOSE_RE = re.compile(r"\n```$")


def chunk_indices(
    cues: List[Cue], max_items: int = 40, max_chars: int = 1500
) -> List[List[int]]:
    """按条数与字符数把 cues 索引分批。"""
    groups: List[List[int]] = []
    current: List[int] = []
    current_chars = 0
    for i, cue in enumerate(cues):
        length = len(str(cue.get("text") or ""))
        if current and (len(current) >= max_items or current_chars + length > max_chars):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(i)
        current_chars += length
    if current:
        groups.append(current)
    return groups


def build_messages(
    texts: List[str], target_lang: str = "中文", context: Optional[str] = None
) -> List[Dict[str, str]]:
    """构造严格要求 JSON 数组对齐输出的对话消息。"""
    system = (
        f"你是专业字幕翻译。将输入 JSON 字符串数组中的每一条翻译成{target_lang}。\n"
        "严格要求：\n"
        "1. 只输出一个 JSON 数组，元素为译文字符串，禁止任何解释、前后缀或代码块围栏。\n"
        "2. 输出数组长度必须与输入完全一致，顺序一一对应。\n"
        "3. 每条独立翻译，不要合并、拆分或增删条目。\n"
        "4. 口语自然、简洁，符合视频语境。"
    )
    if context:
        system += f"\n\n参考背景：{context}"
    user = json.dumps(texts, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_video_context(
    title: str = "", description: str = "", max_chars: int = 600
) -> str:
    """用视频标题与简介拼出翻译参考上下文；两者皆空时返回空串。"""
    parts = []
    title = str(title or "").strip()
    description = str(description or "").strip()
    if title:
        parts.append(f"视频标题：{title}")
    if description:
        desc = description[:max_chars].strip()
        if len(description) > max_chars:
            desc += "…"
        parts.append(f"视频简介：{desc}")
    return "\n".join(parts)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_OPEN_RE.sub("", text)
        text = _FENCE_CLOSE_RE.sub("", text)
    return text.strip()


def _try_json_array(text: str) -> Optional[list]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, list) else None


def parse_translation_response(content: str, expected_n: int) -> Optional[List[str]]:
    """解析译文数组，长度不符或非法则返回 None（触发重试）。"""
    if not content:
        return None
    data = _try_json_array(_strip_code_fence(str(content)))
    if data is None or len(data) != expected_n:
        return None
    return [str(item) for item in data]


def translate_batch(
    texts: List[str],
    call_llm: CallLLM,
    target_lang: str = "中文",
    max_retries: int = 3,
    context: Optional[str] = None,
) -> List[str]:
    """翻译一批文本，最多尝试 ``max_retries`` 次；全败则返回原文降级。"""
    if not texts:
        return []
    messages = build_messages(texts, target_lang, context)
    for _ in range(max(1, max_retries)):
        try:
            content = call_llm(messages)
        except Exception:  # noqa: BLE001 - 网络/接口异常统一触发重试
            continue
        parsed = parse_translation_response(content, len(texts))
        if parsed is not None:
            return parsed
    return list(texts)


def _default_call_llm(
    messages: List[Dict[str, str]],
    *,
    api_key: str,
    model: str,
    base_url: str,
    temperature: float,
    timeout: int,
) -> str:
    body = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature, "stream": False}
    ).encode("utf-8")
    req = urllib.request.Request(
        base_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def resolve_call_llm(
    call_llm: Optional[CallLLM] = None,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.0,
    timeout: int = 60,
) -> CallLLM:
    """返回可用的 ``call_llm``；未注入时基于 DEEPSEEK_API_KEY 构造默认实现。

    供 ``translate_cues`` 与 AI 断句模块共用，避免重复实现 HTTP 请求逻辑。
    """
    if call_llm is not None:
        return call_llm

    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("缺少 DEEPSEEK_API_KEY（可用环境变量或参数传入）")

    def _call(messages: List[Dict[str, str]]) -> str:
        return _default_call_llm(
            messages,
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            timeout=timeout,
        )

    return _call


def translate_cues(
    cues: List[Cue],
    *,
    call_llm: Optional[CallLLM] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    target_lang: str = "中文",
    max_items: int = 40,
    max_chars: int = 1500,
    max_retries: int = 3,
    temperature: float = 0.0,
    timeout: int = 60,
    context: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Cue]:
    """就地填充每条 cue 的 ``translation`` 字段并返回。

    ``progress(done, total)`` 在每批完成后回调，用于上报翻译进度。
    """
    if not cues:
        return cues

    call_llm = resolve_call_llm(
        call_llm,
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
    )

    total = len(cues)
    done = 0
    for group in chunk_indices(cues, max_items, max_chars):
        texts = [str(cues[i].get("text") or "") for i in group]
        translations = translate_batch(
            texts, call_llm, target_lang=target_lang, max_retries=max_retries, context=context
        )
        for i, translation in zip(group, translations):
            cues[i]["translation"] = translation
        done += len(group)
        if progress:
            progress(done, total)

    return cues
