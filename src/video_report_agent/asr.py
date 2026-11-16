"""ASR integration that preserves raw timestamped output."""

from __future__ import annotations

import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from video_report_agent.schemas import AsrSegment

from .redaction import redact

DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"


class AsrError(RuntimeError):
    """Raised when mlx-whisper cannot produce usable timestamped segments."""


@dataclass(frozen=True)
class NormalizedAsrSegments:
    """Indexable segments plus explicit provenance for non-indexed raw rows."""

    segments: tuple[AsrSegment, ...]
    dropped_empty_raw_segment_ordinals: tuple[int, ...]
    dropped_non_positive_raw_segment_ordinals: tuple[int, ...]
    dropped_unrepresentable_raw_segment_ordinals: tuple[int, ...]


@dataclass(frozen=True)
class AsrRun:
    engine: str
    model: str
    language: str
    elapsed_ms: int
    segments: tuple[AsrSegment, ...]
    raw_result: dict[str, Any]
    backend: str = "mlx"
    provider: str = "local"
    timings: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    dropped_empty_raw_segment_ordinals: tuple[int, ...] = ()
    dropped_non_positive_raw_segment_ordinals: tuple[int, ...] = ()
    dropped_unrepresentable_raw_segment_ordinals: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "provider": self.provider,
            "timings": self.timings,
            "usage": self.usage,
            "engine": self.engine,
            "model": self.model,
            "language": self.language,
            "elapsed_ms": self.elapsed_ms,
            "raw_segment_count": self.raw_result.get(
                "raw_segment_count", len(self.raw_result.get("segments", []))
            ),
            "dropped_empty_raw_segment_ordinals": list(self.dropped_empty_raw_segment_ordinals),
            "dropped_non_positive_raw_segment_ordinals": list(
                self.dropped_non_positive_raw_segment_ordinals
            ),
            "dropped_unrepresentable_raw_segment_ordinals": list(
                self.dropped_unrepresentable_raw_segment_ordinals
            ),
            "segments": [segment.model_dump(mode="json") for segment in self.segments],
            "raw_result": redact(self.raw_result),
        }


def _seconds(value: object, field_name: str, ordinal: int) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise AsrError(f"ASR segment {ordinal} has an invalid {field_name}") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise AsrError(f"ASR segment {ordinal} has an invalid non-finite or negative {field_name}")
    return seconds


def normalize_asr_segments(raw_segments: object) -> NormalizedAsrSegments:
    """Quantize non-empty ASR rows to ms without manufacturing overlapping ranges."""

    if not isinstance(raw_segments, list) or not raw_segments:
        raise AsrError("ASR returned no timestamped segments")

    normalized: list[AsrSegment] = []
    dropped_empty_raw_segment_ordinals: list[int] = []
    dropped_non_positive_raw_segment_ordinals: list[int] = []
    dropped_unrepresentable_raw_segment_ordinals: list[int] = []
    previous_raw_end_seconds: float | None = None
    for ordinal, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise AsrError(f"ASR segment {ordinal} is not an object")
        text = raw_segment.get("text")
        if not isinstance(text, str):
            raise AsrError(f"ASR segment {ordinal} has no text")

        start_seconds = _seconds(raw_segment.get("start"), "start", ordinal)
        end_seconds = _seconds(raw_segment.get("end"), "end", ordinal)
        if end_seconds <= start_seconds:
            dropped_non_positive_raw_segment_ordinals.append(ordinal)
            continue
        if (
            previous_raw_end_seconds is not None
            and start_seconds < previous_raw_end_seconds
            and not math.isclose(
                start_seconds,
                previous_raw_end_seconds,
                rel_tol=0,
                abs_tol=1e-9,
            )
        ):
            raise AsrError(f"ASR segments overlap at ordinal {ordinal}")
        previous_raw_end_seconds = end_seconds

        if not text.strip():
            dropped_empty_raw_segment_ordinals.append(ordinal)
            continue
        # Rounding to the nearest millisecond is monotonic for ordered raw ranges.
        # A positive raw range smaller than one representable millisecond is retained
        # in raw_result but excluded from the index instead of expanding its boundary.
        start_ms = round(start_seconds * 1000)
        end_ms = round(end_seconds * 1000)
        if end_ms <= start_ms:
            dropped_unrepresentable_raw_segment_ordinals.append(ordinal)
            continue
        segment = AsrSegment(
            ordinal=ordinal,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            words=[w for w in raw_segment.get("words", []) if isinstance(w, dict)],
        )
        normalized.append(segment)
    if not normalized:
        raise AsrError("ASR returned no non-empty transcript segments")
    return NormalizedAsrSegments(
        segments=tuple(normalized),
        dropped_empty_raw_segment_ordinals=tuple(dropped_empty_raw_segment_ordinals),
        dropped_non_positive_raw_segment_ordinals=tuple(dropped_non_positive_raw_segment_ordinals),
        dropped_unrepresentable_raw_segment_ordinals=tuple(
            dropped_unrepresentable_raw_segment_ordinals
        ),
    )


def _transcribe_mlx(audio_path: Path, *, model: str, language: str = "zh") -> AsrRun:
    """Run mlx-whisper explicitly in Chinese and retain its original result."""

    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise AsrError(f"audio input is missing or empty: {audio_path}")
    if language != "zh":
        raise AsrError("local ASR only permits explicit Chinese ASR")

    try:
        import mlx_whisper
    except ImportError as exc:
        raise AsrError("mlx-whisper is not installed in this project environment") from exc

    started_at = perf_counter()
    try:
        raw_result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=model,
            language=language,
            verbose=False,
        )
    except Exception as exc:
        raise AsrError(f"mlx-whisper transcription failed: {type(exc).__name__}") from exc
    elapsed_ms = round((perf_counter() - started_at) * 1000)

    if not isinstance(raw_result, dict):
        raise AsrError("mlx-whisper returned an invalid transcription payload")
    raw_segments = raw_result.get("segments")
    normalized = normalize_asr_segments(raw_segments)
    return AsrRun(
        engine="mlx-whisper",
        model=model,
        language=language,
        elapsed_ms=elapsed_ms,
        segments=normalized.segments,
        raw_result=raw_result,
        dropped_empty_raw_segment_ordinals=normalized.dropped_empty_raw_segment_ordinals,
        dropped_non_positive_raw_segment_ordinals=(
            normalized.dropped_non_positive_raw_segment_ordinals
        ),
        dropped_unrepresentable_raw_segment_ordinals=(
            normalized.dropped_unrepresentable_raw_segment_ordinals
        ),
    )


class ASRBackend(Protocol):
    default_model: str

    def transcribe(self, audio_path: Path, language: str = "zh") -> AsrRun:
        """Return timestamps relative to audio_path; never apply media offsets."""


class MlxWhisperBackend:
    default_model = DEFAULT_ASR_MODEL

    def __init__(self, model: str = DEFAULT_ASR_MODEL):
        self.model = model

    def transcribe(self, audio_path: Path, language: str = "zh") -> AsrRun:
        # A fresh interpreter keeps MLX/Metal caches out of the long-lived web
        # process. Exiting the executor waits for the worker to release them.
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ) as executor:
            return executor.submit(
                _transcribe_mlx, audio_path, model=self.model, language=language
            ).result()


def transcribe_audio(
    audio_path: Path,
    *,
    model: str,
    language: str = "zh",
    backend: str = "mlx",
    base_url: str | None = None,
    parameters: dict | None = None,
    task_path: Path | None = None,
) -> AsrRun:
    if backend == "mlx":
        return MlxWhisperBackend(model).transcribe(audio_path, language)
    if backend == "paraformer":
        from .paraformer import ParaformerBackend

        return ParaformerBackend(
            model=model, base_url=base_url, parameters=parameters, task_path=task_path
        ).transcribe(
            audio_path, language
        )
    raise AsrError(f"Unknown ASR backend: {backend}")
