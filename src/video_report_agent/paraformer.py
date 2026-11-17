"""DashScope file recognition; one submission, no automatic resubmit."""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from .asr import AsrError, AsrRun, normalize_asr_segments
from .audio import midpoint_pause_ms, probe_audio, split_pcm_audio
from .media_config import DEFAULT_BASE_URL, QWEN_FILETRANS_MODEL, cloud_asr_parameters
from .redaction import redact


class CloudAsrError(AsrError):
    def __init__(self, stage, detail, task_id=None, *, http_status=None, provider_code=None):
        self.http_status = http_status
        self.provider_code = provider_code
        self.stage = stage
        self.task_id = task_id
        self.detail = redact(detail)
        super().__init__(f"DashScope ASR {stage}: {self.detail}")

    def to_dict(self):
        return {"stage": self.stage, "task_id": self.task_id, "detail": self.detail,
                "http_status": self.http_status, "provider_code": self.provider_code}


def provider_code(payload):
    """Keep a bounded code, never a response body, URL, or credential."""
    code = payload.get("code") if isinstance(payload, dict) else None
    if (isinstance(code, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,100}", code)
            and not code.startswith("sk-")):
        return code
    return None


class ParaformerBackend:
    default_model = "paraformer-v2"
    split_min_duration_ms = 60 * 60 * 1000

    def __init__(
        self,
        model=default_model,
        base_url=None,
        *,
        client=None,
        parameters=None,
        wait_seconds=1800,
        poll_seconds=5,
        task_path=None,
    ):
        defaults = cloud_asr_parameters(model)
        self.task_path = task_path
        self.model = model
        self.engine = "qwen-audio" if model == QWEN_FILETRANS_MODEL else "paraformer"
        self.base_url = base_url or DEFAULT_BASE_URL
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is required for paraformer")
        self.parameters = dict(defaults if parameters is None else parameters)
        self.client = client
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds

    def transcribe(self, audio_path: Path, language="zh") -> AsrRun:
        started = time.perf_counter()
        if self.task_path is not None and self.task_path.exists():
            saved = json.loads(self.task_path.read_text())
            if saved.get("task_id") or saved.get("chunks"):
                raise CloudAsrError(
                    "existing_task", "已有云端任务；未重新提交", saved.get("task_id")
                )
        expected_language = "auto" if self.model == QWEN_FILETRANS_MODEL else "zh"
        if language != expected_language:
            raise AsrError(f"{self.model} ASR requires language mode {expected_language!r}")
        if not audio_path.is_file() or not audio_path.stat().st_size:
            raise AsrError("audio input is missing or empty")
        if audio_path.stat().st_size > 1_000_000_000:
            raise AsrError("Paraformer temporary upload exceeds 1 GB")
        duration_ms = probe_audio(audio_path).duration_ms
        if duration_ms > 12 * 60 * 60 * 1000:
            raise AsrError("Paraformer input exceeds 12 hours")
        if duration_ms >= self.split_min_duration_ms:
            split_ms = midpoint_pause_ms(audio_path, duration_ms)
            if split_ms is not None:
                return self._split_run(audio_path, language, split_ms, started)
        return self._single_run(audio_path, language)

    def _single_run(self, audio_path, language):
        if self.client is not None:
            return self._run(self.client, audio_path, language)
        with httpx.Client(timeout=60) as client:
            return self._run(client, audio_path, language)

    def _split_run(self, audio_path, language, split_ms, started):
        from .pipeline import write_json

        offsets = (0, split_ms)
        checkpoints = [
            self.task_path.with_name(f"{self.task_path.stem}.part-{i}.json")
            if self.task_path is not None else None
            for i in range(2)
        ]
        with TemporaryDirectory(prefix=".asr-parts-", dir=audio_path.parent) as directory:
            parts = split_pcm_audio(audio_path, Path(directory), split_ms)
            prepare_ms = round((time.perf_counter() - started) * 1000)
            if self.task_path is not None:
                # Reserve the whole attempt before either submission. Never resubmit
                # the successful sibling after a partial or ambiguous failure.
                write_json(self.task_path, {"chunks": [
                    {"offset_ms": offset, "task_file": checkpoint.name}
                    for offset, checkpoint in zip(offsets, checkpoints)
                ]})

            def recognize(i):
                backend = ParaformerBackend(
                    model=self.model, base_url=self.base_url, client=self.client,
                    parameters=self.parameters, wait_seconds=self.wait_seconds,
                    poll_seconds=self.poll_seconds, task_path=checkpoints[i],
                )
                # Exactly two chunks, including inputs longer than two hours.
                # Do not recursively invoke the splitting entry point.
                return backend._single_run(parts[i], language)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(recognize, i) for i in range(2)]
                results = [future.result() for future in futures]

        merge_started = time.perf_counter()
        segments, chunks = [], []
        dropped = {name: [] for name in (
            "dropped_empty_raw_segment_ordinals", "dropped_non_positive_raw_segment_ordinals",
            "dropped_unrepresentable_raw_segment_ordinals",
        )}
        raw_count = 0
        for offset, result in zip(offsets, results):
            for segment in result.segments:
                segments.append(segment.model_copy(update={
                    "ordinal": segment.ordinal + raw_count,
                    "start_ms": segment.start_ms + offset,
                    "end_ms": segment.end_ms + offset,
                    "words": [
                        {**word, "start": word["start"] + offset / 1000,
                         "end": word["end"] + offset / 1000}
                        for word in segment.words
                    ],
                }))
            for name in dropped:
                dropped[name].extend(value + raw_count for value in getattr(result, name))
            payload = result.to_dict()
            chunks.append({"offset_ms": offset, "result": payload})
            raw_count += payload["raw_segment_count"]

        def total(values):
            return sum(values) if all(value is not None for value in values) else None

        usage_fields = ("duration",)
        if self.model == QWEN_FILETRANS_MODEL:
            usage_fields += ("input_tokens", "output_tokens", "total_tokens")
        usage = {
            "content_duration_ms": total([r.usage.get("content_duration_ms") for r in results]),
            "service_usage": {
                key: total([r.usage.get("service_usage", {}).get(key) for r in results])
                for key in usage_fields
            },
        }
        return AsrRun(
            engine=self.engine, backend="paraformer", provider="dashscope",
            model=self.model, language=language,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            segments=tuple(segments), usage=usage,
            timings={"prepare_ms": prepare_ms,
                     "merge_ms": round((time.perf_counter() - merge_started) * 1000)},
            raw_result={"split_ms": split_ms, "raw_segment_count": raw_count, "chunks": chunks},
            **{name: tuple(values) for name, values in dropped.items()},
        )

    def _run(self, client, path, language):
        started = time.perf_counter()
        stage, task_id = "upload", None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = client.get(
                f"{self.base_url}/uploads",
                headers=headers,
                params={"action": "getPolicy", "model": self.model},
            )
            response.raise_for_status()
            policy = response.json()["data"]
            key = f"{policy['upload_dir']}/{path.name}"
            data = {
                "OSSAccessKeyId": policy["oss_access_key_id"],
                "Signature": policy["signature"],
                "policy": policy["policy"],
                "x-oss-object-acl": policy["x_oss_object_acl"],
                "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
                "key": key,
                "success_action_status": "200",
            }
            with path.open("rb") as audio:
                response = client.post(
                    policy["upload_host"],
                    data=data,
                    files={"file": (path.name, audio, "audio/wav")},
                    timeout=300,
                )
            response.raise_for_status()
            upload_ms = round((time.perf_counter() - started) * 1000)
            stage = "submit"
            try:
                response = client.post(
                    f"{self.base_url}/services/audio/asr/transcription",
                    headers={
                        **headers,
                        "X-DashScope-Async": "enable",
                        "X-DashScope-OssResourceResolve": "enable",
                    },
                    json={
                        "model": self.model,
                        "input": {"file_urls": [f"oss://{key}"]},
                        "parameters": self.parameters,
                    },
                )
            except httpx.TransportError:
                raise CloudAsrError(
                    "submit_unknown", "提交状态未知；未自动重新提交", task_id
                ) from None
            try:
                submitted = response.json()
                task_id = submitted.get("output", {}).get("task_id")
            except (ValueError, AttributeError):
                submitted = {}
            if task_id and self.task_path is not None:
                from .pipeline import write_json

                write_json(self.task_path, {"task_id": task_id})
            if response.status_code >= 500 or (response.is_success and not task_id):
                raise CloudAsrError(
                    "submit_unknown", "提交状态未知；未自动重新提交", task_id,
                    http_status=response.status_code, provider_code=provider_code(submitted),
                )
            response.raise_for_status()
            if not task_id:
                raise CloudAsrError("submit", "Response has no task_id")
            stage = "poll"
            waiting = time.perf_counter()
            while time.perf_counter() - waiting < self.wait_seconds:
                response = client.get(f"{self.base_url}/tasks/{task_id}", headers=headers)
                response.raise_for_status()
                completed = response.json()
                state = completed["output"]["task_status"]
                if state == "SUCCEEDED":
                    break
                if state not in {"PENDING", "RUNNING"}:
                    raise CloudAsrError(stage, {"task_status": state}, task_id,
                                        provider_code=provider_code(completed["output"]))
                time.sleep(
                    min(
                        self.poll_seconds,
                        max(0, self.wait_seconds - (time.perf_counter() - waiting)),
                    )
                )
            else:
                raise CloudAsrError(
                    stage, "Task wait timed out; task may still be running", task_id
                )
            wait_ms = round((time.perf_counter() - waiting) * 1000)
            stage = "result"
            result = completed["output"]["results"][0]
            if result.get("subtask_status") != "SUCCEEDED":
                raise CloudAsrError(
                    stage, {"subtask_status": result.get("subtask_status")}, task_id,
                    provider_code=provider_code(result)
                )
            # Do not send the API key to the result-storage host.
            response = client.get(result["transcription_url"], follow_redirects=True)
            response.raise_for_status()
            payload = response.json()
            transcript = next(t for t in payload["transcripts"] if t["channel_id"] == 0)
            rows = [
                {
                    "start": s["begin_time"] / 1000,
                    "end": s["end_time"] / 1000,
                    "text": s["text"],
                    "words": [
                        {
                            "word": w["text"],
                            "start": w["begin_time"] / 1000,
                            "end": w["end_time"] / 1000,
                        }
                        for w in s.get("words", [])
                    ],
                }
                for s in transcript["sentences"]
            ]
            normalized = normalize_asr_segments(rows)
            return AsrRun(
                engine=self.engine,
                backend="paraformer",
                provider="dashscope",
                model=self.model,
                language=language,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                segments=normalized.segments,
                raw_result=redact(
                    {
                        "task_id": task_id,
                        "response": payload,
                        "task": completed,
                        "raw_segment_count": len(rows),
                    }
                ),
                timings={"upload_ms": upload_ms, "task_wait_ms": wait_ms},
                usage={
                    "content_duration_ms": transcript.get("content_duration_in_milliseconds"),
                    "service_usage": redact(completed.get("usage", {})),
                },
                dropped_empty_raw_segment_ordinals=normalized.dropped_empty_raw_segment_ordinals,
                dropped_non_positive_raw_segment_ordinals=(
                    normalized.dropped_non_positive_raw_segment_ordinals
                ),
                dropped_unrepresentable_raw_segment_ordinals=(
                    normalized.dropped_unrepresentable_raw_segment_ordinals
                ),
            )
        except CloudAsrError:
            raise
        except httpx.HTTPStatusError as exc:
            try:
                code = provider_code(exc.response.json())
            except ValueError:
                code = None
            raise CloudAsrError(stage, "HTTP request rejected", task_id,
                                http_status=exc.response.status_code, provider_code=code) from None
        except Exception as exc:
            # Exception strings can contain signed URLs, request headers, or bodies.
            raise CloudAsrError(stage, type(exc).__name__, task_id) from None
