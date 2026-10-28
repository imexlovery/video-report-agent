import json
from pathlib import Path

import pytest

from video_report_agent import usage


def message(cost=0.12, tokens=120):
    return json.dumps({
        "type": "message_end",
        "message": {
            "role": "assistant", "usage": {"totalTokens": tokens, "cost": {"total": cost}},
        },
    }) + "\n"


def test_cached_matches_full_and_does_not_open_unchanged_log(tmp_path, monkeypatch):
    log = tmp_path / "pi.events.jsonl"
    log.write_text(message() + message(0) + '{"type":"agent_end"}\n')
    expected = usage.call_costs(tmp_path, {}, None)
    assert expected["llm_calls"] == 2
    assert expected["tokens"] == 240
    assert expected["llm_usd_estimate"] == 0.12
    assert expected["llm_unpriced_calls"] == 1
    assert usage.cached_call_costs(tmp_path, {}, None) == expected
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != log, "unchanged Pi log was opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert usage.cached_call_costs(tmp_path, {}, None) == expected


@pytest.mark.parametrize("change", ["append", "truncate", "rewrite", "replace", "delete"])
def test_log_changes_invalidate(tmp_path, change):
    log = tmp_path / "pi.events.jsonl"
    log.write_text(message())
    usage.cached_call_costs(tmp_path, {}, None)
    if change == "append":
        with log.open("a") as stream:
            stream.write(message())
    elif change == "truncate":
        log.write_text("")
    elif change == "rewrite":
        size = log.stat().st_size
        log.write_text(message(0.34))
        assert log.stat().st_size == size
    elif change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_text(message(0.34))
        replacement.replace(log)
    else:
        log.unlink()
    assert usage.cached_call_costs(tmp_path, {}, None) == usage.call_costs(tmp_path, {}, None)


@pytest.mark.parametrize("bad", ["{", "[]", '{"version":1}', "invalid_llm", "version"])
def test_invalid_cache_is_rebuilt(tmp_path, monkeypatch, bad):
    (tmp_path / "pi.events.jsonl").write_text(message())
    expected = usage.cached_call_costs(tmp_path, {}, None)
    path = tmp_path / "usage.json"
    if bad == "version":
        monkeypatch.setattr(usage, "USAGE_CACHE_VERSION", usage.USAGE_CACHE_VERSION + 1)
    elif bad == "invalid_llm":
        cache = json.loads(path.read_text())
        cache["llm"]["tokens"] = "broken"
        path.write_text(json.dumps(cache))
    else:
        path.write_text(bad)
    assert usage.cached_call_costs(tmp_path, {}, None) == expected
    cache = json.loads(path.read_text())
    assert cache["version"] == usage.USAGE_CACHE_VERSION
    assert cache["llm"]["tokens"] == 120


def test_partial_line_is_counted_only_after_newline(tmp_path):
    log = tmp_path / "pi.events.jsonl"
    log.write_text(message() + message().rstrip("\n"))
    assert usage.cached_call_costs(tmp_path, {}, None)["llm_calls"] == 1
    with log.open("a") as stream:
        stream.write("\n")
    assert usage.cached_call_costs(tmp_path, {}, None)["llm_calls"] == 2


def test_append_during_read_does_not_publish_cache(tmp_path, monkeypatch):
    log = tmp_path / "pi.events.jsonl"
    log.write_text(message())
    original = usage._llm_costs

    def append_after_read(run):
        result = original(run)
        with log.open("a") as stream:
            stream.write(message())
        return result

    monkeypatch.setattr(usage, "_llm_costs", append_after_read)
    assert usage.cached_call_costs(tmp_path, {}, None)["llm_calls"] == 1
    assert not (tmp_path / "usage.json").exists()
    monkeypatch.setattr(usage, "_llm_costs", original)
    assert usage.cached_call_costs(tmp_path, {}, None)["llm_calls"] == 2


def test_asr_is_fresh_on_cache_hit(tmp_path, monkeypatch):
    usage.cached_call_costs(tmp_path, {}, None)

    def unexpected(run):
        pytest.fail("LLM cache missed")

    monkeypatch.setattr(usage, "_llm_costs", unexpected)
    asr = tmp_path / "asr.json"
    asr.write_text(json.dumps({
        "backend": "paraformer", "usage": {"content_duration_ms": 60000},
    }))
    original_read = usage.read_object
    asr_reads = []

    def counted_read(path):
        if path == asr:
            asr_reads.append(path)
        return original_read(path)

    monkeypatch.setattr(usage, "read_object", counted_read)
    assert usage.cached_call_costs(tmp_path, {}, 0.0002)["asr_cny_estimate"] == 0.012
    assert usage.cached_call_costs(tmp_path, {}, 0.0004)["asr_cny_estimate"] == 0.024
    assert len(asr_reads) == 1
    assert usage.cached_call_costs(
        tmp_path, {"transcript_reused_from": "old"}, 0.0004,
    )["asr_basis"] == "reused"
    asr.write_text('{"backend":"mlx"}')
    assert usage.cached_call_costs(tmp_path, {}, None)["asr_basis"] == "local"
    assert len(asr_reads) == 2
    assert "asr_cny_estimate" not in json.loads((tmp_path / "usage.json").read_text())["llm"]


def test_cache_write_failure_returns_usage_and_cleans_same_directory_temp(tmp_path, monkeypatch):
    (tmp_path / "pi.events.jsonl").write_text(message())
    expected = usage.call_costs(tmp_path, {}, None)
    seen = []

    def fail_replace(source, target):
        assert source.parent == target.parent == tmp_path
        assert source.is_file()
        seen.append(source)
        raise OSError("read-only destination")

    monkeypatch.setattr(usage.os, "replace", fail_replace)
    assert usage.cached_call_costs(tmp_path, {}, None) == expected
    assert usage.cached_call_costs(tmp_path, {}, None) == expected
    assert len(set(seen)) == 2
    assert all(not path.exists() for path in seen)


def priced_message(model, at="2026-09-21T09:00:00+08:00", **counts):
    from datetime import datetime
    return {"role": "assistant", "provider": "qwenai", "model": model,
            "timestamp": datetime.fromisoformat(at).timestamp() * 1000,
            "usage": {"input": 1000, "output": 100, "cacheRead": 10000,
                      "cacheWrite": 0, **counts}}


@pytest.mark.parametrize("at,factor", [
    ("2026-09-21T07:59:59+08:00", 1), ("2026-09-21T08:00:00+08:00", 2),
    ("2026-09-21T12:00:00+08:00", 2), ("2026-09-21T14:00:00+08:00", 2),
    ("2026-09-21T21:59:59+08:00", 2), ("2026-09-21T22:00:00+08:00", 1),
    ("2026-09-20T10:00:00+08:00", 2), ("2026-10-01T10:00:00+08:00", 2),
])
def test_qwenai_deepseek_official_peak_window_and_holidays(at, factor):
    result = usage.cny_cost(priced_message("deepseek-v4.1-flash", at))
    assert result["total"] == pytest.approx(0.0024 * factor)
    assert result["currency"] == "CNY"


@pytest.mark.parametrize("prompt,rate", [(32768, .2), (32769, .6),
                                        (262144, .6), (262145, 1.2)])
def test_qwen_tiers_include_cached_prompt(prompt, rate):
    result = usage.cny_cost(priced_message("qwen3.7-flash", input=prompt-10000))
    assert result["input"] == pytest.approx((prompt-10000)*rate/1e6)
    assert result["cacheRead"] == pytest.approx(10000*rate*.2/1e6)


def test_qwen38_and_unknown_pricing():
    assert usage.cny_cost(priced_message("qwen3.8-flash"))["total"] == pytest.approx(.00207)
    m = priced_message("deepseek-v4.1-flash")
    del m["timestamp"]
    assert usage.cny_cost(m) is None
    m["provider"] = "another-gateway"
    assert usage.cny_cost(m) is None


def test_cny_backfill_keeps_usd_separate_and_invalidates_old_cache(tmp_path):
    m = priced_message("qwen3.8-flash")
    m["usage"]["cost"] = {"total": 42}
    (tmp_path / "pi.events.jsonl").write_text(
        json.dumps({"type": "message_end", "message": m}) + '\n' + message(.12))
    (tmp_path / "usage.json").write_text('{"version":1}')
    result = usage.cached_call_costs(tmp_path, {}, None)
    assert result["llm_cny_estimate"] == pytest.approx(.00207)
    assert result["llm_usd_estimate"] == .12
    assert result["llm_unpriced_calls"] == 0


def test_visual_usage_is_included_and_invalidates_cached_totals(tmp_path):
    (tmp_path / "visual").mkdir()
    before = usage.cached_call_costs(tmp_path, {}, None)
    (tmp_path / "visual/invocation.json").write_text(
        '{"request_count":1,"status":"completed","model":"deepseek-flash"}')
    (tmp_path / "visual/usage.json").write_text(
        '{"prompt_tokens":100,"prompt_cache_hit_tokens":20,"completion_tokens":30,"total_tokens":130}')
    after = usage.cached_call_costs(tmp_path, {}, None)
    assert after["llm_calls"] == before["llm_calls"] + 1
    assert after["tokens"] == before["tokens"] + 130
    call = usage.execution_statistics(tmp_path, {})["model_calls"][0]
    assert call["stage"] == "visual"
    assert call["message"]["usage"]["cacheRead"] == 20
