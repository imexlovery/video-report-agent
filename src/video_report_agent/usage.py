"""Per-run token and cost estimates shared by benchmarks and callers."""

import json
import math
import os
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

# Bump for schema OR counting/pricing semantics changes, even if fields stay identical.
USAGE_CACHE_VERSION = 3


def read_object(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def is_deepseek_v41_busy_hour(at):
    """QwenAI/Alibaba's daily Beijing busy window for DeepSeek V4.1 Flash (08:00–22:00)."""
    local = datetime.fromtimestamp(at, ZoneInfo("Asia/Shanghai"))
    return 8 <= local.hour < 22


def cny_cost(message, received_at=None):
    """QwenAI Beijing list-price estimate from Pi's disjoint token counters.

    Prices checked 2026-09-21: help.aliyun.com/zh/model-studio/
    {deepseek-v4-1-flash,qwen3-7-flash,qwen3-8-flash}.
    Pi's native cost remains USD; do not relabel or overwrite it.
    """
    if message.get("provider") != "qwenai":
        return None
    model = message.get("model")
    usage = message.get("usage") or {}
    counters = [usage.get(key, 0) for key in ("input", "output", "cacheRead", "cacheWrite")]
    if not all(number(n) for n in counters) or not sum(counters):
        return None
    if "input" not in usage or "output" not in usage:
        return None
    input_tokens, output, cached, written = counters
    period = None
    if model == "deepseek-v4.1-flash":
        # Pi message.timestamp is request-start epoch milliseconds. Alibaba's current
        # pricing page defines the busy window by clock time, without weekday/holiday
        # exceptions: https://help.aliyun.com/zh/model-studio/model-pricing
        timestamp = message.get("timestamp")
        at = timestamp / 1000 if number(timestamp) else received_at
        if not number(at) or written:
            return None
        try:
            peak = is_deepseek_v41_busy_hour(at)
        except (ValueError, OverflowError, OSError, NotImplementedError):
            return None
        factor = 2 if peak else 1
        rates = (1 * factor, 4 * factor, 0.1 * factor, 0)
        period = "peak" if peak else "off_peak"
    elif model in ("qwen3.7-flash", "qwen3.7-flash-2026-07-15"):
        prompt = input_tokens + cached + written
        tier = 0 if prompt <= 32768 else 1 if prompt <= 262144 else 2
        rates = [(0.2, 0.8, 0.04, 0.25), (0.6, 2.4, 0.12, 0.75), (1.2, 4.8, 0.24, 1.5)][tier]
    elif model == "qwen3.8-flash":
        rates = (0.8, 2.7, 0.1, 1.25)
    else:
        return None
    costs = {
        key: n * rate / 1_000_000
        for key, n, rate in zip(("input", "output", "cacheRead", "cacheWrite"), counters, rates)
    }
    return {
        "currency": "CNY",
        "total": sum(costs.values()),
        **costs,
        "period": period,
        "basis": "qwenai_beijing_list_price",
    }


def _llm_costs(run):
    """Count only completed assistant-message events, never streamed usage copies."""
    calls, tokens, estimates, cny_estimates, unknown = 0, 0, [], [], 0
    path = run / "pi.events.jsonl"
    if path.is_file():
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "message_end":
                    continue
                message = event.get("message", {})
                if message.get("role") != "assistant":
                    continue
                calls += 1
                usage = message.get("usage") or {}
                count = usage.get("totalTokens")
                if number(count):
                    tokens += count
                cost = (usage.get("cost") or {}).get("total")
                # Zero-filled custom pricing and failed calls do not prove free usage.
                estimate = cny_cost(message, event.get("_trace_received_at"))
                if estimate is not None:
                    cny_estimates.append(estimate["total"])
                elif number(cost) and cost > 0:
                    estimates.append(cost)
                else:
                    unknown += 1
    return {
        "llm_calls": calls,
        "tokens": tokens,
        "llm_usd_estimate": sum(estimates) if estimates else None,
        "llm_cny_estimate": sum(cny_estimates) if cny_estimates else None,
        "llm_unpriced_calls": unknown,
    }


def _asr_costs(run, status, asr_rate, asr=None):
    if asr is None:
        asr = read_object(run / "asr.json")
    duration = asr.get("usage", {}).get("content_duration_ms")
    if status.get("transcript_reused_from") or status.get("asr_reused_from"):
        asr_cost, asr_basis = 0, "reused"
    elif status.get("asr_performed") is False:
        asr_cost, asr_basis = 0, "not_used"
    elif asr.get("backend") == "mlx":
        asr_cost, asr_basis = 0, "local"
    elif asr.get("backend") == "paraformer" and number(duration) and asr_rate is not None:
        asr_cost, asr_basis = duration / 1000 * asr_rate, "duration_estimate"
    else:
        asr_cost, asr_basis = None, "unknown"
    return {
        "asr_cny_estimate": asr_cost,
        "asr_basis": asr_basis,
    }


def call_costs(run, status, asr_rate):
    """Calculate current estimates without reading or writing a cache."""
    return {**_llm_costs(run), **_asr_costs(run, status, asr_rate)}


def execution_statistics(run, status):
    """Structured facts captured by the trusted host before cancellation removes exports."""
    calls, attempts = [], None
    for name in ("pi.events.jsonl", "model-attempts.jsonl"):
        path = run / name
        if not path.exists():
            continue
        if name == "model-attempts.jsonl":
            attempts = []
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if name == "model-attempts.jsonl":
                    attempts.append(event)
                elif event.get("type") == "message_end":
                    message = event.get("message") or {}
                    if message.get("role") == "assistant":
                        calls.append(
                            {
                                "message": {
                                    key: message[key]
                                    for key in (
                                        "provider",
                                        "model",
                                        "timestamp",
                                        "usage",
                                        "stopReason",
                                    )
                                    if key in message
                                },
                                "received_at": event.get("_trace_received_at"),
                            }
                        )
    pipeline_events = []
    trace = run / "run.trace.jsonl"
    if trace.exists():
        for line in trace.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                pipeline_events.append(event)
    return {
        **call_costs(run, status, None),
        "pipeline_events": pipeline_events,
        "model_calls": calls,
        "model_attempts": attempts,
        "models": sorted(
            {call["message"]["model"] for call in calls if call["message"].get("model")}
        ),
        "duration_ms": read_object(run / "transcript-manifest.json").get("duration_ms"),
        "asr": read_object(run / "asr.json"),
    }


def _signature(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    # Metadata precision depends on the host filesystem; this is not a content hash.
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
    }


@lru_cache(maxsize=512)
def _cached_asr_metadata(path, signature):
    asr = read_object(path)
    return {
        "backend": asr.get("backend"),
        "usage": {"content_duration_ms": (asr.get("usage") or {}).get("content_duration_ms")},
    }


def _valid_llm(value):
    return (
        isinstance(value, dict)
        and set(value)
        == {
            "llm_calls",
            "tokens",
            "llm_usd_estimate",
            "llm_cny_estimate",
            "llm_unpriced_calls",
        }
        and all(number(value[key]) for key in ("llm_calls", "tokens", "llm_unpriced_calls"))
        and all(
            value[key] is None or number(value[key])
            for key in ("llm_usd_estimate", "llm_cny_estimate")
        )
    )


def _write_cache(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=".usage-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
        os.replace(temporary, path)
    except OSError:
        pass  # Caching is optional; a write failure must not hide computed usage.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def cached_call_costs(run, status, asr_rate):
    """Reuse LLM estimates and ASR metadata; recalculate ASR cost for each call."""
    path = run / "pi.events.jsonl"
    signature = _signature(path)
    cache_path = run / "usage.json"
    cache = read_object(cache_path)
    llm = cache.get("llm")
    if not (
        cache.get("version") == USAGE_CACHE_VERSION
        and "source" in cache
        and cache["source"] == signature
        and _valid_llm(llm)
    ):
        llm = _llm_costs(run)
        if signature == _signature(path):
            _write_cache(
                cache_path,
                {
                    "version": USAGE_CACHE_VERSION,
                    "source": signature,
                    "llm": llm,
                },
            )
    asr_path = run / "asr.json"
    signature = _signature(asr_path)
    metadata = _cached_asr_metadata(asr_path, tuple(signature.items()) if signature else None)
    return {**llm, **_asr_costs(run, status, asr_rate, metadata)}
