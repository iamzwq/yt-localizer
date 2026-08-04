from app.translate import (
    build_messages,
    build_video_context,
    chunk_indices,
    parse_translation_response,
    translate_batch,
    translate_cues,
)


def _cues(texts):
    return [{"start": i * 1000, "end": i * 1000 + 900, "text": t} for i, t in enumerate(texts)]


def test_build_video_context_combines_title_and_description():
    ctx = build_video_context("标题A", "这是简介")
    assert "标题A" in ctx and "这是简介" in ctx


def test_build_video_context_truncates_long_description():
    ctx = build_video_context("T", "x" * 1000, max_chars=100)
    assert ctx.endswith("…")
    assert len(ctx) < 200


def test_build_video_context_empty_returns_blank():
    assert build_video_context("", "") == ""


def test_build_video_context_title_only():
    assert build_video_context("仅标题", "") == "视频标题：仅标题"


def test_context_injected_into_system_prompt():
    messages = build_messages(["hi"], context=build_video_context("标题X", "简介Y"))
    assert "标题X" in messages[0]["content"]
    assert "简介Y" in messages[0]["content"]


def test_chunk_indices_by_count():
    cues = _cues([f"line{i}" for i in range(5)])
    groups = chunk_indices(cues, max_items=2, max_chars=10000)
    assert groups == [[0, 1], [2, 3], [4]]


def test_chunk_indices_by_chars():
    cues = _cues(["a" * 40, "b" * 40, "c" * 40])
    groups = chunk_indices(cues, max_items=100, max_chars=50)
    assert groups == [[0], [1], [2]]


def test_parse_translation_response_plain_array():
    assert parse_translation_response('["你好","世界"]', 2) == ["你好", "世界"]


def test_parse_translation_response_strips_code_fence():
    content = '```json\n["你好","世界"]\n```'
    assert parse_translation_response(content, 2) == ["你好", "世界"]


def test_parse_translation_response_extracts_array_with_noise():
    content = '这是结果：["你好","世界"] 完毕'
    assert parse_translation_response(content, 2) == ["你好", "世界"]


def test_parse_translation_response_rejects_length_mismatch():
    assert parse_translation_response('["你好"]', 2) is None


def test_parse_translation_response_rejects_garbage():
    assert parse_translation_response("not json", 2) is None
    assert parse_translation_response("", 2) is None


def test_build_messages_shape():
    messages = build_messages(["hello"], context="tech talk")
    assert messages[0]["role"] == "system"
    assert "tech talk" in messages[0]["content"]
    assert messages[1]["content"] == '["hello"]'


def test_translate_batch_success():
    def call_llm(_messages):
        return '["你好","世界"]'

    assert translate_batch(["hello", "world"], call_llm) == ["你好", "世界"]


def test_translate_batch_retries_then_succeeds():
    calls = {"n": 0}

    def call_llm(_messages):
        calls["n"] += 1
        if calls["n"] < 3:
            return "bad response"
        return '["你好","世界"]'

    result = translate_batch(["hello", "world"], call_llm, max_retries=3)
    assert result == ["你好", "世界"]
    assert calls["n"] == 3


def test_translate_batch_falls_back_to_source_after_all_retries():
    def call_llm(_messages):
        raise RuntimeError("network down")

    result = translate_batch(["hello", "world"], call_llm, max_retries=3)
    assert result == ["hello", "world"]


def test_translate_batch_exception_then_success_counts_as_retry():
    calls = {"n": 0}

    def call_llm(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return '["你好"]'

    assert translate_batch(["hello"], call_llm, max_retries=3) == ["你好"]
    assert calls["n"] == 2


def test_translate_cues_fills_translation_across_batches():
    def call_llm(messages):
        import json

        texts = json.loads(messages[1]["content"])
        return json.dumps([t.upper() for t in texts], ensure_ascii=False)

    cues = _cues(["a", "b", "c"])
    translate_cues(cues, call_llm=call_llm, max_items=2)
    assert [c["translation"] for c in cues] == ["A", "B", "C"]


def test_translate_cues_empty():
    assert translate_cues([], call_llm=lambda m: "[]") == []
