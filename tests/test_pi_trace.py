"""Compact trace retains diagnostics and billing without streamed duplicates."""
import asyncio
import json
import os
import time

import pytest
from test_pi import Process

from video_report_agent.pi import PiError, PiRunner
from video_report_agent.pi_trace import compact_event
from video_report_agent.retention import cleanup_media
from video_report_agent.usage import _llm_costs


def test_compact_trace_preserves_tool_payload_usage_and_completion(tmp_path, monkeypatch):
    monkeypatch.setenv('PI_TRACE_FULL', '1')
    usage = {'totalTokens': 42, 'cost': {'total': 0.2}}
    message = {'role': 'assistant', 'stopReason': 'stop', 'usage': usage,
               'content': [{'type': 'thinking', 'thinking': 'private reasoning'},
                           {'type': 'toolCall', 'id': 't1', 'name': 'write',
                            'arguments': {'path': 'report.html', 'content': '<html>full</html>'}},
                           {'type': 'text', 'text': 'done'}]}
    events = [
        {'type': 'request_start', 'request_id': 1, 'timestamp': 10},
        {'type': 'message_start', 'message': {'role': 'assistant', 'content': []}},
        {'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'd'}},
        {'type': 'request_first_response', 'request_id': 1, 'latency_ms': 123},
        {'type': 'message_end', 'message': message},
        {'type': 'tool_execution_start', 'toolCallId': 't1', 'toolName': 'write',
         'args': message['content'][1]['arguments']},
        {'type': 'tool_execution_update', 'partialResult': {'content': 'partial'}},
        {'type': 'tool_execution_end', 'toolCallId': 't1', 'result': {'content': 'saved'}},
        {'type': 'message_end', 'message': {'role': 'toolResult', 'content': 'saved'}},
        {'type': 'turn_end', 'message': message, 'toolResults': ['saved']},
        {'type': 'agent_end', 'messages': [message]},
        {'type': 'agent_settled'},
    ]
    asyncio.run(PiRunner()._consume(Process(events), tmp_path, 'task'))
    saved = [json.loads(line) for line in (tmp_path / 'pi.events.jsonl').read_text().splitlines()]
    assert len(saved) == len(events) - 3
    assert not any(e['type'].endswith('_update') for e in saved)
    final = next(e['message'] for e in saved if e['type'] == 'message_end')
    assert final['usage'] == usage
    assert final['content'][0] == {'type': 'thinking', 'characters': 17}
    assert 'arguments' not in final['content'][1]
    tool_input = next(e['args'] for e in saved if e['type'] == 'tool_execution_start')
    assert tool_input['content'] == '<html>full</html>'
    assert 'messages' not in next(e for e in saved if e['type'] == 'agent_end')
    compact_cost = _llm_costs(tmp_path)
    (tmp_path / 'pi.events.jsonl').write_bytes((tmp_path / 'pi.raw.events.jsonl').read_bytes())
    assert _llm_costs(tmp_path) == compact_cost
    assert message['content'][0]['thinking'] == 'private reasoning'  # input never mutated


def test_default_does_not_write_raw_log(tmp_path, monkeypatch):
    monkeypatch.delenv('PI_TRACE_FULL', raising=False)
    events = [{'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}},
              {'type': 'agent_settled'}]
    asyncio.run(PiRunner()._consume(Process(events), tmp_path, 'task'))
    assert not (tmp_path / 'pi.raw.events.jsonl').exists()
    assert compact_event({'type': 'auto_retry_start', 'attempt': 2})['attempt'] == 2


def test_style_model_failure_does_not_persist_provider_error(tmp_path, monkeypatch):
    monkeypatch.setenv('PI_TRACE_FULL', '1')
    events = [
        {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'error',
                                            'errorMessage': 'private provider diagnostic',
                                            'content': [{'type': 'text',
                                                         'text': 'private provider diagnostic'}]}},
        {'type': 'agent_settled'},
    ]
    with pytest.raises(PiError):
        asyncio.run(PiRunner()._consume(Process(events), tmp_path, 'task', agent_role='style'))
    assert 'private provider diagnostic' not in (tmp_path / 'pi.events.jsonl').read_text()
    assert not (tmp_path / 'pi.raw.events.jsonl').exists()


def test_raw_retention_only_expires_terminal_debug_logs(tmp_path, monkeypatch):
    monkeypatch.setenv('MEDIA_KEEP_LAST', '0')
    monkeypatch.setenv('MEDIA_MAX_AGE_DAYS', '0')
    monkeypatch.setenv('PI_TRACE_FULL_MAX_AGE_DAYS', '7')
    for state in ('RENDERED', 'FAILED', 'RUNNING'):
        run = tmp_path / state
        run.mkdir()
        (run / 'status.json').write_text(json.dumps({'state': state}))
        old = time.time() - 8 * 86400
        os.utime(run / 'status.json', (old, old))
        (run / 'pi.raw.events.jsonl').write_text('raw')
        (run / 'pi.events.jsonl').write_text('compact')
        (run / 'report.html').write_text('report')
    from storage_fixtures import import_fixtures
    import_fixtures(tmp_path)
    cleanup_media(tmp_path)
    for state in ('RENDERED', 'FAILED', 'RUNNING'):
        run = tmp_path / state
        assert (run / 'pi.raw.events.jsonl').exists() == (state == 'RUNNING')
        assert (run / 'pi.events.jsonl').read_text() == 'compact'
        assert (run / 'report.html').exists()
