"""Local, source-preserving transcript foundation for Visual Report V1.1.

The module deliberately keeps acquisition, normalization, alignment, and the
small local fusion rule together.  It does not make semantic decisions: ASR
segments are the legacy continuity anchors; selected-source mode can use subtitle cues.
Every canonical character comes
from an ASR, subtitle, or OCR event (apart from named presentation
normalization).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol
from unicodedata import normalize as unicode_normalize

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from video_report_agent.audio import probe_media_duration_ms

TRANSCRIPT_SCHEMA_VERSION = "visual-report-transcript-foundation.v1"
NORMALIZER_VERSION = "nfkc-whitespace-punctuation-opencc-t2s.v1"
FUSION_VERSION = "local-span-fusion.v1"
OCR_VERSION = "rapidocr-ppocrv6-small-onnxruntime.v1"
OCR_ENGINE = "onnxruntime"
OCR_DETECTION_MODEL = "PP-OCRv6-small"
OCR_RECOGNITION_MODEL = "PP-OCRv6-small"
ALIGNMENT_JITTER_MS = 1_500
OCR_MIN_CONFIDENCE = 0.55
LOCAL_DIFF_SEPARATOR_MIN_CHARACTERS = 3
LOCAL_CONTEXT_MIN_CHARACTERS = 4
LOCAL_SPAN_MAX_CHARACTERS = 8
OCR_VISUAL_CHANGE_THRESHOLD = 0.035
OCR_TEXT_SIMILARITY_THRESHOLD = 0.86
OCR_MAX_MERGE_GAP_MS = 1_500
SUPPORTED_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt", ".ass", ".ssa"})
SUPPORTED_SUBTITLE_CODECS = {
    "subrip": ".srt",
    "webvtt": ".vtt",
    "ass": ".ass",
    "ssa": ".ass",
    "mov_text": ".srt",
}

SourceKind = Literal["asr", "subtitle_track", "bilibili_ai", "ocr"]
TranscriptMode = Literal["asr-only", "fused", "selected"]


class TranscriptFoundationError(RuntimeError):
    """Raised when a required transcript foundation input is invalid."""


class OcrError(TranscriptFoundationError):
    """Raised when local OCR or frame acquisition cannot produce a result."""


class SubtitleParseError(TranscriptFoundationError):
    """Raised for an unsupported or malformed optional subtitle source."""


class OCRBackend(Protocol):
    provider: str
    model: str

    def recognize(self, image_path: Path) -> OcrResult:
        """Return detections in input-image pixel coordinates."""


class Stability(BaseModel):
    """Cross-sample OCR stability without combining source confidences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int = Field(ge=1)
    distinct_frame_count: int = Field(ge=1)


class SourceTextEvent(BaseModel):
    """One timestamped source observation retained without overwriting raw text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    source: SourceKind
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    text: str
    normalized_text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    stability: Stability = Field(
        default_factory=lambda: Stability(sample_count=1, distinct_frame_count=1)
    )
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source event text must not be blank")
        return value

    @field_validator("normalized_text")
    @classmethod
    def normalized_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source event normalized_text must not be blank")
        return value

    @model_validator(mode="after")
    def validate_boundaries_and_normalization(self) -> "SourceTextEvent":
        if self.end_ms <= self.start_ms:
            raise ValueError("source event end_ms must be greater than start_ms")
        if self.normalized_text != normalize_text(self.text):
            raise ValueError("source event normalized_text is not deterministic")
        return self


class TranscriptUnit(BaseModel):
    """A timestamped canonical transcript unit with its actual source provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    canonical_text: str
    asr_text: str | None = None
    ocr_text: str | None = None
    subtitle_text: str | None = None
    bilibili_ai_text: str | None = None
    resolution: str = Field(min_length=1)
    flags: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unit(self) -> "TranscriptUnit":
        if self.end_ms <= self.start_ms:
            raise ValueError("transcript unit end_ms must be greater than start_ms")
        if not self.canonical_text.strip() or (
            self.asr_text is not None and not self.asr_text.strip()
        ):
            raise ValueError("transcript unit text must not be blank")
        return self


class TranscriptManifest(BaseModel):
    """Small run-local summary for the transcript foundation artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = TRANSCRIPT_SCHEMA_VERSION
    video_id: str = Field(min_length=1)
    duration_ms: int = Field(gt=0)
    transcript_mode: TranscriptMode
    source_status: dict[str, dict[str, Any]]
    canonical_unit_count: int = Field(ge=1)
    normalizer_version: str = NORMALIZER_VERSION
    fusion_version: str = FUSION_VERSION
    status: Literal["READY", "DEGRADED", "FAILED"]
    warnings: tuple[str, ...] = ()
    artifacts: dict[str, str] = Field(default_factory=dict)
    selected_sources: tuple[SourceKind, ...] = ()
    primary_source: SourceKind | None = None


@dataclass(frozen=True)
class SampledFrame:
    timestamp_ms: int
    path: Path


@dataclass(frozen=True)
class OcrDetection:
    text: str
    confidence: float | None
    points: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class OcrResult:
    detections: tuple[OcrDetection, ...]
    provider: str
    model: str


@dataclass(frozen=True)
class _OcrSample:
    timestamp_ms: int
    frame_path: Path
    text: str
    normalized_text: str
    confidence: float | None
    candidate_id: str = ""


@dataclass(frozen=True)
class AutoRoiResult:
    roi: tuple[float, float, float, float] | None
    status: Literal["READY", "UNSTABLE"]
    warnings: tuple[str, ...] = ()
    candidates: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SubtitleDiscovery:
    status: Literal["AVAILABLE", "ABSENT", "UNSUPPORTED", "FAILED"]
    path: Path | None = None
    format: str | None = None
    track_index: int | None = None
    language: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class TranscriptBuild:
    source_events: tuple[SourceTextEvent, ...]
    canonical_units: tuple[TranscriptUnit, ...]
    manifest: TranscriptManifest


def _opencc_simplify(value: str) -> str:
    return _opencc_converter().convert(value)


@lru_cache(maxsize=1)
def _opencc_converter() -> Any:
    try:
        from opencc import OpenCC
    except ImportError as exc:  # pragma: no cover - project dependency is locked
        raise TranscriptFoundationError("OpenCC is not installed") from exc
    return OpenCC("t2s")


_PUNCTUATION_SPACING = re.compile(r"\s+([，。！？；：、》」』）】])")
_OPENING_SPACING = re.compile(r"([《「『（【])\s+")
_CJK_SPACING = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
    r"\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
)
_ASS_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")


def _presentation_text(value: str) -> str:
    text = unicode_normalize("NFKC", value).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = _PUNCTUATION_SPACING.sub(r"\1", text)
    text = _OPENING_SPACING.sub(r"\1", text)
    text = _CJK_SPACING.sub("", text)
    return text


def normalize_text(value: str) -> str:
    """Return closed comparison text; no lexical content is generated."""

    return _opencc_simplify(_presentation_text(value))


def parse_roi(value: object) -> tuple[float, float, float, float]:
    """Parse normalized ``x1,y1,x2,y2`` ROI coordinates."""

    raw: Sequence[object]
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (tuple, list)):
        raw = value
    else:
        raise OcrError("ROI must contain four normalized coordinates")
    if len(raw) != 4:
        raise OcrError("ROI must contain four normalized coordinates")
    try:
        coordinates = tuple(float(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise OcrError("ROI coordinates must be numeric") from exc
    if not all(math.isfinite(item) for item in coordinates):
        raise OcrError("ROI coordinates must be finite")
    x1, y1, x2, y2 = coordinates
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise OcrError("ROI must satisfy 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
    return coordinates  # type: ignore[return-value]


def roi_pixel_bounds(
    width: int, height: int, roi: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    """Convert a normalized ROI to a containing, half-open pixel rectangle."""

    if width <= 0 or height <= 0:
        raise OcrError("image dimensions must be positive")
    x1, y1, x2, y2 = parse_roi(roi)
    left = max(0, min(width - 1, math.floor(width * x1)))
    top = max(0, min(height - 1, math.floor(height * y1)))
    right = max(left + 1, min(width, math.ceil(width * x2)))
    bottom = max(top + 1, min(height, math.ceil(height * y2)))
    if right <= left or bottom <= top:
        raise OcrError("ROI does not contain any pixels")
    return left, top, right, bottom


def _require_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - locked RapidOCR dependency
        raise OcrError("OpenCV is required for local frame sampling") from exc
    return cv2


def _image_fingerprint(image_path: Path, roi: tuple[float, float, float, float]) -> Any:
    cv2 = _require_cv2()
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise OcrError(f"sampled frame cannot be decoded: {image_path}")
    height, width = image.shape[:2]
    left, top, right, bottom = roi_pixel_bounds(width, height, roi)
    crop = image[top:bottom, left:right]
    return cv2.resize(crop, (32, 16), interpolation=cv2.INTER_AREA).astype("float32") / 255.0


def filter_changed_frames(
    frames: Iterable[SampledFrame],
    roi: tuple[float, float, float, float],
    *,
    threshold: float = OCR_VISUAL_CHANGE_THRESHOLD,
) -> list[SampledFrame]:
    """Keep the first frame and frames with a perceptible ROI change."""

    if threshold < 0:
        raise OcrError("visual-change threshold must not be negative")
    retained: list[SampledFrame] = []
    previous: Any | None = None
    for frame in frames:
        fingerprint = _image_fingerprint(frame.path, roi)
        if previous is None or float(abs(fingerprint - previous).mean()) >= threshold:
            retained.append(frame)
            previous = fingerprint
    return retained


def _require_ffmpeg() -> str:
    import shutil

    executable = shutil.which("ffmpeg")
    if executable is None:
        raise OcrError("ffmpeg is required for local video sampling")
    return executable


def sample_video_frames(
    video_path: Path,
    *,
    sample_fps: float,
    start_ms: int = 0,
    end_ms: int | None = None,
    output_dir: Path,
    runner: Callable[..., Any] | None = None,
) -> list[SampledFrame]:
    """Extract sparse JPEG samples into a caller-owned temporary directory."""

    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise OcrError(f"video input is missing or empty: {video_path}")
    if not math.isfinite(sample_fps) or sample_fps <= 0 or sample_fps > 10:
        raise OcrError("sample_fps must be greater than zero and no more than 10")
    if start_ms < 0:
        raise OcrError("start_ms must not be negative")
    if end_ms is None:
        end_ms = probe_media_duration_ms(video_path)
    if end_ms <= start_ms:
        raise OcrError("sample range must be non-empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_ffmpeg()
    duration_seconds = (end_ms - start_ms) / 1000
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration_seconds:.3f}",
        "-an",
        "-vf",
        f"fps={sample_fps:g}",
        "-q:v",
        "3",
        str(output_dir / "frame-%06d.jpg"),
    ]
    completed = (runner or subprocess.run)(
        command, capture_output=True, text=True, check=False, shell=False
    )
    if getattr(completed, "returncode", 1) != 0:
        raise OcrError("ffmpeg frame sampling failed")
    paths = sorted(output_dir.glob("frame-*.jpg"))
    if not paths:
        raise OcrError("frame sampling produced no frames")
    interval_ms = 1000 / sample_fps
    frames = [
        SampledFrame(
            timestamp_ms=start_ms + round(index * interval_ms),
            path=path,
        )
        for index, path in enumerate(paths)
    ]
    return [frame for frame in frames if frame.timestamp_ms < end_ms]


def _crop_frame(
    frame_path: Path, roi: tuple[float, float, float, float], output_path: Path
) -> None:
    cv2 = _require_cv2()
    image = cv2.imread(str(frame_path), cv2.IMREAD_COLOR)
    if image is None:
        raise OcrError(f"sampled frame cannot be decoded: {frame_path}")
    height, width = image.shape[:2]
    left, top, right, bottom = roi_pixel_bounds(width, height, roi)
    if not cv2.imwrite(str(output_path), image[top:bottom, left:right]):
        raise OcrError(f"cannot write temporary OCR crop: {output_path}")


def _coerce_points(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x1, y1, x2, y2 = (float(item) for item in value)
        return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    points: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                points.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                return ()
    return tuple(points)


def _coerce_detection(row: object) -> OcrDetection | None:
    if isinstance(row, OcrDetection):
        return row
    if isinstance(row, dict):
        text = row.get("text") or row.get("transcription")
        confidence = row.get("confidence", row.get("score"))
        points = row.get("box", row.get("points", ()))
    elif isinstance(row, (list, tuple)) and len(row) >= 2:
        points, text_value = row[0], row[1]
        if isinstance(text_value, (list, tuple)) and text_value and isinstance(text_value[0], str):
            text = text_value[0]
            confidence = text_value[1] if len(text_value) >= 2 else None
        else:
            text = text_value
            confidence = row[2] if len(row) >= 3 else None
    else:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        score = None if confidence is None else float(confidence)
    except (TypeError, ValueError):
        score = None
    return OcrDetection(text=text, confidence=score, points=_coerce_points(points))


def _recognize(adapter: OCRBackend, image_path: Path) -> list[OcrDetection]:
    try:
        return list(adapter.recognize(image_path).detections)
    except Exception as exc:
        raise OcrError(f"OCR failed: {type(exc).__name__}") from exc


def normalize_ocr_result(result: object) -> list[OcrDetection]:
    """Convert RapidOCR output at the provider boundary."""
    if result is None:
        return []
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is not None:
        result = [
            OcrDetection(
                text=text,
                confidence=(scores[index] if scores is not None else None),
                points=_coerce_points(boxes[index].tolist() if boxes is not None else ()),
            )
            for index, text in enumerate(texts)
            if isinstance(text, str) and text.strip()
        ]
    if isinstance(result, tuple) and result and isinstance(result[0], (list, tuple)):
        result = result[0]
    if not isinstance(result, (list, tuple)):
        return []
    detections = [_coerce_detection(row) for row in result]
    return [detection for detection in detections if detection is not None]


class RapidOcrBackend:
    """Lazy local RapidOCR adapter; no image or text leaves the process."""

    provider = "rapidocr"
    model = "PP-OCRv6-small-onnxruntime"

    def __init__(self) -> None:
        try:
            from rapidocr import (
                EngineType,
                LangDet,
                LangRec,
                ModelType,
                OCRVersion,
                RapidOCR,
            )
        except ImportError as exc:  # pragma: no cover - dependency is locked
            raise OcrError("rapidocr is not installed") from exc
        self._engine = RapidOCR(
            params={
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.lang_type": LangDet.CH,
                "Det.model_type": ModelType.SMALL,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.lang_type": LangRec.CH,
                "Rec.model_type": ModelType.SMALL,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
            }
        )

    def recognize(self, image_path: Path) -> OcrResult:
        return OcrResult(
            tuple(normalize_ocr_result(self._engine(str(image_path)))),
            self.provider,
            self.model,
        )


def _ocr_text(detections: Sequence[OcrDetection]) -> tuple[str, float | None]:
    def sort_key(item: OcrDetection) -> tuple[float, float, str]:
        if item.points:
            x = min(point[0] for point in item.points)
            y = min(point[1] for point in item.points)
        else:
            x = y = 0
        return y, x, item.text

    ordered = sorted(detections, key=sort_key)
    text = _presentation_text(" ".join(item.text for item in ordered))
    scores = [item.confidence for item in ordered if item.confidence is not None]
    return text, (min(scores) if scores else None)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _candidate_row(sample: _OcrSample, roi: tuple[float, float, float, float]) -> dict[str, Any]:
    return {
        "candidate_id": sample.candidate_id,
        "timestamp_ms": sample.timestamp_ms,
        "observed_text": sample.text,
        "normalized_text": sample.normalized_text,
        "confidence": sample.confidence,
        "roi": list(roi),
        "frame_id": sample.frame_path.name,
        "sample_id": sample.frame_path.stem,
    }


def _identify_ocr_samples(samples: Sequence[_OcrSample]) -> list[_OcrSample]:
    ordered = sorted(samples, key=lambda item: (item.timestamp_ms, item.frame_path.name))
    identified: list[_OcrSample] = []
    for index, sample in enumerate(ordered, start=1):
        candidate_id = sample.candidate_id or f"ocr-frame-{index:06d}"
        identified.append(
            _OcrSample(
                timestamp_ms=sample.timestamp_ms,
                frame_path=sample.frame_path,
                text=sample.text,
                normalized_text=sample.normalized_text,
                confidence=sample.confidence,
                candidate_id=candidate_id,
            )
        )
    return identified


def _consensus_for_group(
    group: Sequence[_OcrSample],
) -> tuple[_OcrSample, list[_OcrSample], int, bool]:
    """Choose one observed candidate and report its simple winning cluster."""

    considered = list(group[:5])
    clusters: list[list[_OcrSample]] = []
    representatives: list[_OcrSample] = []
    for sample in considered:
        matching = [
            (index, _similarity(sample.normalized_text, representative.normalized_text))
            for index, representative in enumerate(representatives)
            if _similarity(sample.normalized_text, representative.normalized_text)
            >= OCR_TEXT_SIMILARITY_THRESHOLD
        ]
        if not matching:
            clusters.append([sample])
            representatives.append(sample)
            continue
        cluster_index = max(matching, key=lambda item: (item[1], -item[0]))[0]
        clusters[cluster_index].append(sample)

    def medoid(cluster: Sequence[_OcrSample]) -> _OcrSample:
        return sorted(
            cluster,
            key=lambda item: (
                -sum(_similarity(item.normalized_text, other.normalized_text) for other in cluster),
                -(item.confidence if item.confidence is not None else 0),
                item.timestamp_ms,
                item.candidate_id,
            ),
        )[0]

    summaries = [(len(cluster), medoid(cluster), cluster) for cluster in clusters]
    summaries.sort(key=lambda item: (-item[0], item[1].candidate_id))
    support, representative, winning_cluster = summaries[0]
    second_support = summaries[1][0] if len(summaries) > 1 else 0
    unique_winner = support >= 2 and support > second_support
    return representative, winning_cluster, support, unique_winner


def _merge_ocr_samples(
    samples: Sequence[_OcrSample],
    *,
    roi: tuple[float, float, float, float],
    sample_fps: float,
    duration_ms: int | None,
) -> list[SourceTextEvent]:
    ordered = _identify_ocr_samples(samples)
    groups: list[list[_OcrSample]] = []
    max_gap_ms = max(OCR_MAX_MERGE_GAP_MS, math.ceil(2000 / sample_fps))
    for sample in ordered:
        if groups:
            previous = groups[-1][-1]
            if (
                sample.timestamp_ms - previous.timestamp_ms <= max_gap_ms
                and _similarity(sample.normalized_text, previous.normalized_text)
                >= OCR_TEXT_SIMILARITY_THRESHOLD
            ):
                groups[-1].append(sample)
                continue
        groups.append([sample])

    events: list[SourceTextEvent] = []
    frame_span_ms = math.ceil(1000 / sample_fps)
    for index, group in enumerate(groups, start=1):
        best, consensus_cluster, consensus_support, consensus_unique = _consensus_for_group(group)
        start_ms = group[0].timestamp_ms
        end_ms = group[-1].timestamp_ms + frame_span_ms
        if duration_ms is not None:
            end_ms = min(end_ms, duration_ms)
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        confidence_values = [item.confidence for item in group if item.confidence is not None]
        confidence = min(confidence_values) if confidence_values else None
        events.append(
            SourceTextEvent(
                event_id=f"ocr-{index:06d}",
                source="ocr",
                start_ms=start_ms,
                end_ms=end_ms,
                text=best.text,
                normalized_text=normalize_text(best.text),
                confidence=confidence,
                stability=Stability(
                    sample_count=len(group),
                    distinct_frame_count=len({item.frame_path.name for item in group}),
                ),
                provenance={
                    "roi": list(roi),
                    "frame_timestamps_ms": [item.timestamp_ms for item in group],
                    "ocr_confidences": confidence_values,
                    "candidate_ids": [item.candidate_id for item in group],
                    "consensus_candidate_ids": [item.candidate_id for item in consensus_cluster],
                    "representative_candidate_id": best.candidate_id,
                    "consensus_support": consensus_support,
                    "consensus_unique": consensus_unique,
                    "consensus_eligible": consensus_unique,
                    "observed_variants": list(dict.fromkeys(item.text for item in group)),
                    "frame_candidates": [_candidate_row(item, roi) for item in group],
                    "sampling": "sparse_visual_change_filtered_perceptual_dedup",
                    "ocr_version": OCR_VERSION,
                    "ocr_engine": OCR_ENGINE,
                    "ocr_detection_model": OCR_DETECTION_MODEL,
                    "ocr_recognition_model": OCR_RECOGNITION_MODEL,
                },
            )
        )
    return events


def extract_subtitle_ocr_events(
    video_path: Path,
    roi: tuple[float, float, float, float] | str,
    *,
    sample_fps: float = 2.0,
    start_ms: int = 0,
    end_ms: int | None = None,
    duration_ms: int | None = None,
    adapter: OCRBackend | None = None,
    runner: Callable[..., Any] | None = None,
    roi_mode: Literal["explicit", "auto"] = "explicit",
) -> list[SourceTextEvent]:
    """Run explicit-ROI local OCR and return stable, timestamped events."""

    normalized_roi = parse_roi(roi)
    video_duration_ms = duration_ms or probe_media_duration_ms(video_path)
    processed_end_ms = video_duration_ms if end_ms is None else min(end_ms, video_duration_ms)
    if start_ms >= processed_end_ms:
        raise OcrError("sample range must overlap the video")
    processed_duration_ms = processed_end_ms - start_ms
    coverage_ratio = processed_duration_ms / video_duration_ms
    coverage_status = (
        "FULL" if start_ms == 0 and processed_end_ms == video_duration_ms else "PARTIAL"
    )
    active_adapter = adapter or RapidOcrBackend()
    with tempfile.TemporaryDirectory(prefix="visual-report-ocr-") as temporary_root:
        temporary_dir = Path(temporary_root)
        frames = sample_video_frames(
            video_path,
            sample_fps=sample_fps,
            start_ms=start_ms,
            end_ms=processed_end_ms,
            output_dir=temporary_dir / "frames",
            runner=runner,
        )
        changed_frames = filter_changed_frames(frames, normalized_roi)
        samples: list[_OcrSample] = []
        for index, frame in enumerate(changed_frames):
            crop_path = temporary_dir / f"crop-{index:06d}.jpg"
            _crop_frame(frame.path, normalized_roi, crop_path)
            detections = _recognize(active_adapter, crop_path)
            text, confidence = _ocr_text(detections)
            if not text or (confidence is not None and confidence < OCR_MIN_CONFIDENCE):
                continue
            samples.append(
                _OcrSample(
                    timestamp_ms=frame.timestamp_ms,
                    frame_path=frame.path,
                    text=text,
                    normalized_text=normalize_text(text),
                    confidence=confidence,
                )
            )
        events = _merge_ocr_samples(
            samples,
            roi=normalized_roi,
            sample_fps=sample_fps,
            duration_ms=video_duration_ms,
        )
        return [
            event.model_copy(
                update={
                    "provenance": {
                        **event.provenance,
                        "provider": active_adapter.provider,
                        "model": active_adapter.model,
                        "roi_mode": roi_mode,
                        "processed_start_ms": start_ms,
                        "processed_end_ms": processed_end_ms,
                        "video_duration_ms": video_duration_ms,
                        "coverage_ratio": round(coverage_ratio, 6),
                        "coverage_status": coverage_status,
                    }
                }
            )
            for event in events
        ]


def _image_size(path: Path) -> tuple[int, int]:
    cv2 = _require_cv2()
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise OcrError(f"sampled frame cannot be decoded: {path}")
    height, width = image.shape[:2]
    return width, height


def detect_auto_roi(
    video_path: Path,
    *,
    sample_fps: float = 1.0,
    start_ms: int = 0,
    end_ms: int | None = None,
    adapter: OCRBackend | None = None,
    runner: Callable[..., Any] | None = None,
) -> AutoRoiResult:
    """Find one recurring sentence-like horizontal OCR band, if clearly usable."""

    active_adapter = adapter or RapidOcrBackend()
    full_roi = (0.0, 0.0, 1.0, 1.0)
    with tempfile.TemporaryDirectory(prefix="visual-report-auto-roi-") as temporary_root:
        frames = sample_video_frames(
            video_path,
            sample_fps=sample_fps,
            start_ms=start_ms,
            end_ms=end_ms,
            output_dir=Path(temporary_root) / "frames",
            runner=runner,
        )
        changed_frames = filter_changed_frames(frames, full_roi)
        observations: list[dict[str, Any]] = []
        for frame in changed_frames:
            width, height = _image_size(frame.path)
            for detection in _recognize(active_adapter, frame.path):
                if not detection.points:
                    continue
                xs = [point[0] / width for point in detection.points]
                ys = [point[1] / height for point in detection.points]
                text = _presentation_text(detection.text)
                if not text:
                    continue
                observations.append(
                    {
                        "timestamp_ms": frame.timestamp_ms,
                        "text": text,
                        "normalized_text": normalize_text(text),
                        "x1": max(0.0, min(xs)),
                        "x2": min(1.0, max(xs)),
                        "y1": max(0.0, min(ys)),
                        "y2": min(1.0, max(ys)),
                        "confidence": detection.confidence,
                    }
                )

        bands: list[list[dict[str, Any]]] = []
        for observation in sorted(
            observations,
            key=lambda item: (item["y1"], item["x1"], item["timestamp_ms"], item["text"]),
        ):
            y_center = (observation["y1"] + observation["y2"]) / 2
            matching = [
                band
                for band in bands
                if abs(y_center - sum((item["y1"] + item["y2"]) / 2 for item in band) / len(band))
                <= 0.065
            ]
            if matching:
                min_band = min(matching, key=lambda band: len(band))
                min_band.append(observation)
            else:
                bands.append([observation])

        candidates: list[dict[str, Any]] = []
        for band in bands:
            timestamps = {item["timestamp_ms"] for item in band}
            texts = {item["normalized_text"] for item in band}
            x1 = min(item["x1"] for item in band)
            x2 = max(item["x2"] for item in band)
            y1 = min(item["y1"] for item in band)
            y2 = max(item["y2"] for item in band)
            width = x2 - x1
            height = y2 - y1
            x_center = (x1 + x2) / 2
            y_center = (y1 + y2) / 2
            average_length = sum(len(item["normalized_text"]) for item in band) / len(band)
            if (
                len(timestamps) < 3
                or len(texts) < 2
                or average_length < 3
                or width < 0.12
                or height < 0.008
                or height > 0.20
                or width > 0.92
                or not 0.12 <= x_center <= 0.88
                or not 0.55 <= y_center <= 0.98
                or (x_center < 0.22 or x_center > 0.78)
                and width < 0.25
            ):
                continue
            roi = (
                max(0.0, x1 - 0.02),
                max(0.0, y1 - 0.025),
                min(1.0, x2 + 0.02),
                min(1.0, y2 + 0.045),
            )
            recurrence = min(1.0, len(timestamps) / 5)
            change = min(1.0, len(texts) / 4)
            centered = 1.0 - min(1.0, abs(x_center - 0.5) / 0.5)
            bottom = min(1.0, max(0.0, (y_center - 0.35) / 0.55))
            sentence = min(1.0, average_length / 14)
            score = (
                recurrence * 0.28
                + change * 0.28
                + centered * 0.18
                + bottom * 0.10
                + sentence * 0.16
            )
            candidates.append(
                {
                    "roi": list(roi),
                    "score": round(score, 6),
                    "sample_count": len(timestamps),
                    "distinct_text_count": len(texts),
                    "average_text_length": round(average_length, 3),
                    "y_center": round(y_center, 4),
                }
            )
        candidates.sort(key=lambda item: (-item["score"], item["roi"]))
        if not candidates or candidates[0]["score"] < 0.42:
            return AutoRoiResult(
                roi=None,
                status="UNSTABLE",
                warnings=("AUTO_ROI_UNSTABLE",),
                candidates=tuple(candidates[:5]),
            )
        return AutoRoiResult(
            roi=parse_roi(candidates[0]["roi"]),
            status="READY",
            candidates=tuple(candidates[:5]),
        )


def _subtitle_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUBTITLE_SUFFIXES:
        raise SubtitleParseError(f"unsupported subtitle format: {suffix or 'unknown'}")
    return suffix.lstrip(".")


def parse_subtitle_file(path: Path) -> list[SourceTextEvent]:
    """Parse SRT/VTT/ASS cues through one local pysubs2 adapter."""

    if not path.is_file() or path.stat().st_size == 0:
        raise SubtitleParseError(f"subtitle file is missing or empty: {path}")
    subtitle_format = _subtitle_format(path)
    try:
        import pysubs2

        subtitles = pysubs2.load(str(path), encoding="utf-8")
    except Exception as exc:
        raise SubtitleParseError(f"subtitle file could not be parsed: {path}") from exc
    events: list[SourceTextEvent] = []
    for index, cue in enumerate(subtitles, start=1):
        raw_text = str(getattr(cue, "text", ""))
        raw_text = _HTML_TAG.sub("", _ASS_TAG.sub("", raw_text)).replace("\\N", " ")
        text = _presentation_text(raw_text)
        if not text:
            continue
        try:
            start_ms = int(cue.start)
            end_ms = int(cue.end)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SubtitleParseError(f"subtitle cue {index} has invalid timing") from exc
        if start_ms < 0 or end_ms <= start_ms:
            raise SubtitleParseError(f"subtitle cue {index} has invalid timing")
        events.append(
            SourceTextEvent(
                event_id=f"subtitle-{len(events) + 1:06d}",
                source="subtitle_track",
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                normalized_text=normalize_text(text),
                confidence=None,
                stability=Stability(sample_count=1, distinct_frame_count=1),
                provenance={
                    "path": str(path.resolve()),
                    "format": subtitle_format,
                    "cue_index": index,
                },
            )
        )
    if not events:
        raise SubtitleParseError(f"subtitle file contains no usable cues: {path}")
    return events


def _subtitle_name_score(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    chinese = any(token in name for token in ("zh", "chi", "zho", "chs", "中文", "简中", "繁中"))
    english = any(token in name for token in ("en", "eng", "英文"))
    return (2 if chinese else (0 if not english else -1), name)


def select_subtitle_file(paths: Iterable[Path]) -> Path | None:
    candidates = sorted(
        (
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUBTITLE_SUFFIXES
        ),
        key=lambda path: (-_subtitle_name_score(path)[0], _subtitle_name_score(path)[1]),
    )
    return candidates[0] if candidates else None


def _probe_subtitle_streams(
    media_path: Path, runner: Callable[..., Any] | None
) -> list[dict[str, Any]]:
    import shutil

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise SubtitleParseError("ffprobe is unavailable for subtitle discovery")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title",
        "-of",
        "json",
        str(media_path),
    ]
    completed = (runner or subprocess.run)(
        command, capture_output=True, text=True, check=False, shell=False
    )
    if getattr(completed, "returncode", 1) != 0:
        raise SubtitleParseError("ffprobe subtitle discovery failed")
    try:
        payload = json.loads(getattr(completed, "stdout", ""))
    except json.JSONDecodeError as exc:
        raise SubtitleParseError("ffprobe subtitle discovery returned invalid JSON") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    return (
        [stream for stream in streams if isinstance(stream, dict)]
        if isinstance(streams, list)
        else []
    )


def _stream_language_score(stream: dict[str, Any]) -> tuple[int, str]:
    tags = stream.get("tags")
    tags = tags if isinstance(tags, dict) else {}
    language = str(tags.get("language", "")).lower()
    title = str(tags.get("title", "")).lower()
    chinese = any(
        token in f"{language} {title}" for token in ("zh", "chi", "zho", "中文", "简", "繁")
    )
    return (2 if chinese else 0, f"{language}:{title}")


def discover_subtitle_source(
    media_path: Path,
    *,
    explicit_path: Path | None = None,
    artifact_root: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> SubtitleDiscovery:
    """Choose an explicit/sidecar/embedded text subtitle deterministically."""

    if explicit_path is not None:
        if not explicit_path.is_file():
            return SubtitleDiscovery(status="FAILED", warning="SUBTITLE_FILE_MISSING")
        try:
            _subtitle_format(explicit_path)
        except SubtitleParseError as exc:
            return SubtitleDiscovery(status="FAILED", warning=str(exc))
        return SubtitleDiscovery(
            status="AVAILABLE",
            path=explicit_path.resolve(),
            format=explicit_path.suffix.lower().lstrip("."),
        )

    try:
        sidecar = select_subtitle_file(
            candidate
            for candidate in media_path.parent.iterdir()
            if candidate.name.startswith(f"{media_path.stem}.")
        )
    except OSError:
        return SubtitleDiscovery(status="FAILED", warning="SUBTITLE_DISCOVERY_FAILED")
    if sidecar is not None:
        return SubtitleDiscovery(
            status="AVAILABLE",
            path=sidecar.resolve(),
            format=sidecar.suffix.lower().lstrip("."),
        )

    try:
        streams = _probe_subtitle_streams(media_path, runner)
    except SubtitleParseError:
        return SubtitleDiscovery(status="FAILED", warning="SUBTITLE_DISCOVERY_FAILED")
    if not streams:
        return SubtitleDiscovery(status="ABSENT", warning="SUBTITLE_ABSENT")
    ordered = sorted(
        enumerate(streams),
        key=lambda item: (
            -_stream_language_score(item[1])[0],
            item[0],
            _stream_language_score(item[1])[1],
        ),
    )
    stream_position, selected = ordered[0]
    codec = str(selected.get("codec_name", "")).lower()
    suffix = SUPPORTED_SUBTITLE_CODECS.get(codec)
    if suffix is None:
        return SubtitleDiscovery(status="UNSUPPORTED", warning="SUBTITLE_UNSUPPORTED")
    if artifact_root is None:
        return SubtitleDiscovery(status="FAILED", warning="SUBTITLE_OUTPUT_ROOT_MISSING")
    artifact_root.mkdir(parents=True, exist_ok=True)
    output_path = artifact_root / f"embedded-subtitle-{stream_position:02d}{suffix}"
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return SubtitleDiscovery(status="FAILED", warning="SUBTITLE_EXTRACTION_FAILED")
    subtitle_codec = "srt" if codec == "mov_text" else "copy"
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-map",
        f"0:s:{stream_position}",
        "-c:s",
        subtitle_codec,
        str(output_path),
    ]
    completed = (runner or subprocess.run)(
        command, capture_output=True, text=True, check=False, shell=False
    )
    if getattr(completed, "returncode", 1) != 0 or not output_path.is_file():
        return SubtitleDiscovery(status="FAILED", warning="SUBTITLE_EXTRACTION_FAILED")
    tags = selected.get("tags") if isinstance(selected.get("tags"), dict) else {}
    language = str(tags.get("language", "")).strip() or None
    return SubtitleDiscovery(
        status="AVAILABLE",
        path=output_path.resolve(),
        format=suffix.lstrip("."),
        track_index=stream_position,
        language=language,
    )


def _event_from_row(row: object, *, expected_source: SourceKind | None = None) -> SourceTextEvent:
    if not isinstance(row, dict):
        raise TranscriptFoundationError("source event row must be an object")
    try:
        event = SourceTextEvent.model_validate(row)
    except ValueError as exc:
        raise TranscriptFoundationError("source event row is invalid") from exc
    if expected_source is not None and event.source != expected_source:
        raise TranscriptFoundationError(
            f"source event {event.event_id} is not a {expected_source} event"
        )
    return event


def load_event_jsonl(
    path: Path, *, expected_source: SourceKind | None = None
) -> list[SourceTextEvent]:
    if not path.is_file():
        raise TranscriptFoundationError(f"source event file is missing: {path}")
    events: list[SourceTextEvent] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TranscriptFoundationError(f"source event file cannot be read: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            events.append(_event_from_row(json.loads(line), expected_source=expected_source))
        except (json.JSONDecodeError, TranscriptFoundationError) as exc:
            raise TranscriptFoundationError(
                f"invalid source event at {path}:{line_number}"
            ) from exc
    if not events:
        raise TranscriptFoundationError(f"source event file contains no events: {path}")
    return events


def asr_events_from_payload(
    payload: dict[str, Any], *, payload_path: Path | None = None
) -> list[SourceTextEvent]:
    rows = payload.get("segments")
    if not isinstance(rows, list) or not rows:
        raise TranscriptFoundationError("ASR payload contains no segments")
    events: list[SourceTextEvent] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TranscriptFoundationError(f"ASR segment {index} is invalid")
        try:
            ordinal = int(row.get("ordinal", index))
            start_ms = int(row["start_ms"])
            end_ms = int(row["end_ms"])
            text_value = row["text"]
            if not isinstance(text_value, str) or not text_value.strip():
                raise TypeError("ASR text must be a non-empty string")
            text = text_value
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscriptFoundationError(f"ASR segment {index} is invalid") from exc
        provenance: dict[str, Any] = {"asr_ordinal": ordinal}
        provenance.update({key: payload.get(key) for key in ("backend", "provider", "model")})
        words = row.get("words")
        if words:
            provenance["word_timestamps"] = words
        if payload_path is not None:
            provenance["path"] = str(payload_path.resolve())
        events.append(
            SourceTextEvent(
                event_id=f"asr-{ordinal:06d}",
                source="asr",
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                normalized_text=normalize_text(text),
                provenance=provenance,
            )
        )
    return sorted(events, key=lambda event: (event.start_ms, event.end_ms, event.event_id))


def _aligned(left: SourceTextEvent, right: SourceTextEvent, jitter_ms: int) -> bool:
    return left.start_ms <= right.end_ms + jitter_ms and right.start_ms <= left.end_ms + jitter_ms


def _alignment_score(
    asr_event: SourceTextEvent, optional_event: SourceTextEvent
) -> tuple[int, int, float, int, int, str]:
    overlap_ms = max(
        0,
        min(asr_event.end_ms, optional_event.end_ms)
        - max(asr_event.start_ms, optional_event.start_ms),
    )
    gap_ms = max(
        0,
        max(asr_event.start_ms, optional_event.start_ms)
        - min(asr_event.end_ms, optional_event.end_ms),
    )
    center_distance = abs(
        (asr_event.start_ms + asr_event.end_ms) - (optional_event.start_ms + optional_event.end_ms)
    )
    return (
        1 if overlap_ms else 0,
        -gap_ms,
        _similarity(normalize_text(asr_event.text), normalize_text(optional_event.text)),
        -center_distance,
        -asr_event.start_ms,
        asr_event.event_id,
    )


def _temporal_anchor_index(
    asr_events: Sequence[SourceTextEvent], optional_event: SourceTextEvent
) -> int:
    def key(index: int) -> tuple[int, int, int, int, str]:
        event = asr_events[index]
        overlap_ms = max(
            0,
            min(event.end_ms, optional_event.end_ms) - max(event.start_ms, optional_event.start_ms),
        )
        gap_ms = max(
            0,
            max(event.start_ms, optional_event.start_ms) - min(event.end_ms, optional_event.end_ms),
        )
        center_distance = abs(
            (event.start_ms + event.end_ms) - (optional_event.start_ms + optional_event.end_ms)
        )
        return (
            0 if overlap_ms else 1,
            -overlap_ms,
            gap_ms,
            center_distance,
            event.event_id,
        )

    return min(range(len(asr_events)), key=key)


def _assign_optional_events(
    asr_events: Sequence[SourceTextEvent],
    optional_events: Sequence[SourceTextEvent],
) -> dict[str, list[SourceTextEvent]]:
    """Assign each optional event to one closest ASR anchor.

    A cue that touches two adjacent ASR intervals at a jitter boundary must not
    contaminate both units. Choosing one deterministic best anchor is the
    conservative interpretation of the V1.1 fine-grained contract.
    """

    assigned: dict[str, list[SourceTextEvent]] = {event.event_id: [] for event in asr_events}
    for optional_event in optional_events:
        anchor_index = _temporal_anchor_index(asr_events, optional_event)
        neighbor_indices = range(max(0, anchor_index - 1), min(len(asr_events), anchor_index + 2))
        candidates = [
            asr_events[index]
            for index in neighbor_indices
            if _aligned(asr_events[index], optional_event, ALIGNMENT_JITTER_MS)
        ]
        if not candidates:
            continue
        best = max(candidates, key=lambda event: _alignment_score(event, optional_event))
        assigned[best.event_id].append(optional_event)
    for events in assigned.values():
        events.sort(key=lambda event: (event.start_ms, event.end_ms, event.event_id))
    return assigned


def find_local_replacement(asr_text: str, candidate_text: str) -> list[dict[str, Any]]:
    """Return independently judgeable local diff spans.

    Changes separated by a sufficiently long equal run are independent. Nearby
    changes remain one mixed span so fusion cannot accept only the attractive
    part of an inseparable OCR candidate.
    """

    left = _presentation_text(asr_text)
    right = _presentation_text(candidate_text)
    if left == right:
        return []
    opcodes = SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
    changes = [index for index, opcode in enumerate(opcodes) if opcode[0] != "equal"]
    if not changes:
        return []

    groups: list[list[int]] = [[changes[0]]]
    for change_index in changes[1:]:
        between = opcodes[groups[-1][-1] + 1 : change_index]
        independently_separated = any(
            opcode[0] == "equal"
            and min(opcode[2] - opcode[1], opcode[4] - opcode[3])
            >= LOCAL_DIFF_SEPARATOR_MIN_CHARACTERS
            for opcode in between
        )
        if independently_separated:
            groups.append([change_index])
        else:
            groups[-1].append(change_index)

    spans: list[dict[str, Any]] = []
    for group in groups:
        first_change = group[0]
        last_change = group[-1]
        left_start = opcodes[first_change][1]
        left_end = opcodes[last_change][2]
        right_start = opcodes[first_change][3]
        right_end = opcodes[last_change][4]
        unchanged_context = left_start + len(left) - left_end
        asr_span = left[left_start:left_end]
        candidate_span = right[right_start:right_end]
        if asr_span and candidate_span:
            operation = "replace"
        elif candidate_span:
            operation = "insert"
        else:
            operation = "delete"
        spans.append(
            {
                "operation": operation,
                "asr_start": left_start,
                "asr_end": left_end,
                "asr_span": asr_span,
                "candidate_span": candidate_span,
                "unchanged_context": unchanged_context,
                "left_context": left_start,
                "right_context": len(left) - left_end,
                "eligible_context": unchanged_context >= 4,
                "candidate_similarity": _similarity(left, right),
            }
        )
    return spans


def _choose_event(events: Sequence[SourceTextEvent]) -> SourceTextEvent | None:
    if not events:
        return None
    return sorted(
        events,
        key=lambda event: (
            -event.stability.sample_count,
            -(event.confidence if event.confidence is not None else 0),
            -len(event.normalized_text),
            event.event_id,
        ),
    )[0]


def _ocr_is_stable(event: SourceTextEvent) -> bool:
    multi_frame = (
        event.stability.sample_count >= 2
        and event.stability.distinct_frame_count >= 2
        and (event.confidence is None or event.confidence >= OCR_MIN_CONFIDENCE)
    )
    if not multi_frame:
        return False
    if event.provenance.get("consensus_eligible") is False:
        return False
    consensus_support = event.provenance.get("consensus_support", event.stability.sample_count)
    if not isinstance(consensus_support, int) or consensus_support < 2:
        return False
    if event.provenance.get("consensus_unique") is False:
        return False
    consensus_candidate_ids = event.provenance.get("consensus_candidate_ids")
    return not (isinstance(consensus_candidate_ids, list) and len(consensus_candidate_ids) < 2)


def _ocr_frame_consensus_matches_span(
    asr_text: str, event: SourceTextEvent, target_span: dict[str, Any]
) -> bool:
    """Require every retained consensus frame to support the same local diff.

    This keeps a stable correction from borrowing confidence from an adjacent
    insertion/deletion or another frame-level change in the same OCR sample.
    """

    raw_candidates = event.provenance.get("frame_candidates")
    if not isinstance(raw_candidates, list):
        return True
    candidate_ids = _consensus_candidate_ids(event)
    candidates = {
        row.get("candidate_id"): row
        for row in raw_candidates
        if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)
    }
    if len(candidate_ids) < 2 or any(
        candidate_id not in candidates for candidate_id in candidate_ids
    ):
        return False
    target_key = (
        target_span["operation"],
        target_span["asr_start"],
        target_span["asr_end"],
        normalize_text(target_span["candidate_span"]),
    )
    target_start = target_span["asr_start"]
    target_end = target_span["asr_end"]
    target_support = 0
    for candidate_id in candidate_ids:
        candidate_text = candidates[candidate_id].get("observed_text")
        if not isinstance(candidate_text, str):
            return False
        spans = find_local_replacement(asr_text, candidate_text)
        keys = tuple(
            sorted(
                (
                    span["operation"],
                    span["asr_start"],
                    span["asr_end"],
                    normalize_text(span["candidate_span"]),
                )
                for span in spans
            )
        )
        if not keys or target_key not in keys:
            continue
        target_support += 1
        target_contaminated = False
        for span in spans:
            span_key = (
                span["operation"],
                span["asr_start"],
                span["asr_end"],
                normalize_text(span["candidate_span"]),
            )
            if span_key == target_key:
                continue
            span_start = span["asr_start"]
            span_end = span["asr_end"]
            gap = (
                target_start - span_end
                if span_end <= target_start
                else span_start - target_end
                if target_end <= span_start
                else 0
            )
            if gap < LOCAL_DIFF_SEPARATOR_MIN_CHARACTERS:
                target_contaminated = True
                break
        if target_contaminated:
            target_support -= 1
    return target_support >= 2


def _is_cjk_span(value: str) -> bool:
    ranges = (
        ("\u3400", "\u4dbf"),
        ("\u4e00", "\u9fff"),
        ("\uf900", "\ufaff"),
    )
    return bool(value) and all(any(start <= char <= end for start, end in ranges) for char in value)


def _requires_repeated_cjk_support(asr_span: str, candidate_span: str) -> bool:
    asr_value = normalize_text(asr_span)
    candidate_value = normalize_text(candidate_span)
    return (
        len(asr_value) > 1
        and len(candidate_value) > 1
        and _is_cjk_span(asr_value)
        and _is_cjk_span(candidate_value)
    )


def _span_is_eligible(
    event: SourceTextEvent,
    span: dict[str, Any],
    *,
    asr_text: str | None = None,
    repeated_candidate_support: Mapping[str, int] | None = None,
) -> bool:
    if (
        not span["eligible_context"]
        or span["unchanged_context"] < LOCAL_CONTEXT_MIN_CHARACTERS
        or span["candidate_similarity"] < 0.60
    ):
        return False
    asr_span = span["asr_span"]
    candidate_span = span["candidate_span"]
    if (
        span["operation"] != "replace"
        or not asr_span
        or not candidate_span
        or (event.source == "ocr" and any(char.isspace() for char in asr_span + candidate_span))
        or any(
            not 1 <= len(normalize_text(value)) <= LOCAL_SPAN_MAX_CHARACTERS
            for value in (asr_span, candidate_span)
        )
    ):
        return False
    if event.source == "ocr" and not _ocr_is_stable(event):
        return False
    if (
        event.source == "ocr"
        and asr_text is not None
        and not _ocr_frame_consensus_matches_span(asr_text, event, span)
    ):
        return False
    if (
        event.source == "ocr"
        and isinstance(event.provenance.get("frame_candidates"), list)
        and repeated_candidate_support is not None
        and _requires_repeated_cjk_support(asr_span, candidate_span)
        and repeated_candidate_support.get(normalize_text(candidate_span), 0) < 2
    ):
        return False
    return True


def _asr_ordinals(event: SourceTextEvent) -> list[int]:
    ordinal = event.provenance.get("asr_ordinal")
    return [ordinal] if isinstance(ordinal, int) and ordinal >= 0 else []


def _consensus_candidate_ids(event: SourceTextEvent) -> list[str]:
    for key in ("consensus_candidate_ids", "candidate_ids"):
        value = event.provenance.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
    return []


def _span_record(
    event: SourceTextEvent, span: dict[str, Any], *, reason: str | None = None
) -> dict[str, Any]:
    record = {
        **span,
        "event_id": event.event_id,
        "source": event.source,
    }
    candidate_ids = _consensus_candidate_ids(event)
    if candidate_ids:
        record["candidate_ids"] = candidate_ids
    consensus_support = event.provenance.get("consensus_support", event.stability.sample_count)
    if isinstance(consensus_support, int):
        record["consensus_support"] = consensus_support
    if reason is not None:
        record["reason"] = reason
    return record


def _repeated_ocr_candidate_support(
    asr_events: Sequence[SourceTextEvent],
    assignments: Mapping[str, Sequence[SourceTextEvent]],
) -> dict[str, int]:
    """Count independently aligned OCR events supporting a CJK replacement."""

    support: dict[str, set[str]] = {}
    for asr_event in asr_events:
        for event in assignments.get(asr_event.event_id, ()):
            if not _ocr_is_stable(event):
                continue
            for span in find_local_replacement(asr_event.text, event.text):
                if (
                    span["operation"] != "replace"
                    or not span["eligible_context"]
                    or not _requires_repeated_cjk_support(span["asr_span"], span["candidate_span"])
                    or not _ocr_frame_consensus_matches_span(asr_event.text, event, span)
                ):
                    continue
                candidate = normalize_text(span["candidate_span"])
                support.setdefault(candidate, set()).add(event.event_id)
    return {candidate: len(event_ids) for candidate, event_ids in support.items()}


def _build_unit(
    index: int,
    asr_event: SourceTextEvent,
    subtitle_events: Sequence[SourceTextEvent],
    ocr_events: Sequence[SourceTextEvent],
    repeated_candidate_support: Mapping[str, int] | None = None,
) -> TranscriptUnit:
    all_events = [asr_event, *subtitle_events, *ocr_events]
    subtitle = _choose_event([e for e in all_events if e.source == "subtitle_track"])
    ai_subtitle = _choose_event([e for e in all_events if e.source == "bilibili_ai"])
    actual_asr = _choose_event([e for e in all_events if e.source == "asr"])
    base_source = asr_event.source
    ocr = _choose_event(ocr_events)
    asr_text = _presentation_text(asr_event.text)
    candidate_events = [*subtitle_events, *ocr_events]
    candidate_ids = [event.event_id for event in candidate_events]
    provenance: dict[str, Any] = {
        "asr_event_ids": [actual_asr.event_id] if actual_asr else [],
        "asr_backend": actual_asr.provenance.get("backend") if actual_asr else None,
        "asr_provider": actual_asr.provenance.get("provider") if actual_asr else None,
        "asr_model": actual_asr.provenance.get("model") if actual_asr else None,
        "candidate_event_ids": candidate_ids,
        "asr_ordinals": _asr_ordinals(actual_asr) if actual_asr else [],
        "canonical_text_source": base_source,
        "anchor_event_id": asr_event.event_id,
        "anchor_source": base_source,
        "normalization_version": NORMALIZER_VERSION,
    }
    flags: list[str] = []
    resolution = f"{base_source}_only"
    canonical_text = asr_text
    selected_source_ids: list[str] = []

    asr_norm = normalize_text(asr_event.text)
    subtitle_values = {event.normalized_text for event in subtitle_events}
    stable_ocr_events = [event for event in ocr_events if _ocr_is_stable(event)]
    stable_ocr_values = {event.normalized_text for event in stable_ocr_events}
    reliable_values = subtitle_values | stable_ocr_values
    ocr_outlier_events = [event for event in ocr_events if event.normalized_text != asr_norm]
    if ocr_outlier_events and asr_norm in (subtitle_values | stable_ocr_values):
        resolution = "asr_preserved_ocr_rejected"
        flags.append("OCR_REJECTED")
        provenance["rejected_event_ids"] = [event.event_id for event in ocr_outlier_events]
    elif reliable_values and reliable_values == {asr_norm}:
        resolution = "sources_agree"
    elif asr_norm in reliable_values and any(value != asr_norm for value in reliable_values):
        resolution = "unresolved"
        flags.append("UNRESOLVED")
    else:
        correction_candidates = [
            event
            for event in [*subtitle_events, *stable_ocr_events]
            if event.normalized_text != asr_norm
        ]
        span_candidates = [
            (event, span)
            for event in correction_candidates
            for span in find_local_replacement(asr_event.text, event.text)
        ]
        accepted = [
            (event, span)
            for event, span in span_candidates
            if _span_is_eligible(
                event,
                span,
                asr_text=asr_event.text,
                repeated_candidate_support=repeated_candidate_support,
            )
        ]
        unresolved = [
            _span_record(event, span)
            for event, span in span_candidates
            if not _span_is_eligible(
                event,
                span,
                asr_text=asr_event.text,
                repeated_candidate_support=repeated_candidate_support,
            )
            and span["eligible_context"]
        ]
        proposals: dict[tuple[int, int], set[str]] = {}
        for _, span in accepted:
            proposals.setdefault((span["asr_start"], span["asr_end"]), set()).add(
                span["candidate_span"]
            )
        conflicting_ranges = {key for key, values in proposals.items() if len(values) > 1}
        if conflicting_ranges:
            unresolved.extend(
                _span_record(event, span, reason="conflicting_candidates")
                for event, span in accepted
                if (span["asr_start"], span["asr_end"]) in conflicting_ranges
            )
            accepted = [
                (event, span)
                for event, span in accepted
                if (span["asr_start"], span["asr_end"]) not in conflicting_ranges
            ]

        if accepted:
            unique_accepted: dict[tuple[int, int, str], tuple[SourceTextEvent, dict[str, Any]]] = {}
            for event, span in accepted:
                key = (span["asr_start"], span["asr_end"], span["candidate_span"])
                unique_accepted.setdefault(key, (event, span))
            ordered_accepted = sorted(
                unique_accepted.values(), key=lambda item: item[1]["asr_start"], reverse=True
            )
            for _, span in ordered_accepted:
                canonical_text = (
                    canonical_text[: span["asr_start"]]
                    + span["candidate_span"]
                    + canonical_text[span["asr_end"] :]
                )
            selected_source_ids = list(dict.fromkeys(event.event_id for event, _ in accepted))
            has_subtitle = any(event.source == "subtitle_track" for event, _ in accepted)
            has_ocr = any(event.source == "ocr" for event, _ in accepted)
            if has_subtitle and has_ocr:
                resolution = "subtitle_ocr_corrected_asr"
            elif has_subtitle:
                resolution = "subtitle_corrected_asr"
            else:
                resolution = "ocr_corrected_asr"
            provenance["canonical_text_source"] = (
                "subtitle_track+ocr"
                if has_subtitle and has_ocr
                else ("subtitle_track" if has_subtitle else "ocr")
            )
            correction_sources = sorted({event.source for event, _ in accepted})
            if base_source != "asr" or "bilibili_ai" in correction_sources:
                provenance["canonical_text_source"] = "+".join(correction_sources)
                resolution = f'{"+".join(correction_sources)}_corrected_{base_source}'
            provenance["selected_source_event_ids"] = selected_source_ids
            accepted_spans = [
                _span_record(event, span)
                for event, span in sorted(accepted, key=lambda item: item[1]["asr_start"])
            ]
            provenance["accepted_spans"] = accepted_spans
            if len(accepted_spans) == 1:
                provenance["replacement"] = {
                    "asr_span": accepted_spans[0]["asr_span"],
                    "candidate_span": accepted_spans[0]["candidate_span"],
                    "canonical_text": canonical_text,
                }
            if unresolved:
                flags.append("UNRESOLVED")
                provenance["unresolved_spans"] = unresolved
            rejected_ocr = [
                candidate
                for candidate in ocr_events
                if candidate.event_id not in selected_source_ids
                and candidate.normalized_text != asr_norm
            ]
            if rejected_ocr:
                flags.append("OCR_REJECTED")
                provenance["rejected_event_ids"] = [
                    candidate.event_id for candidate in rejected_ocr
                ]
        elif any(
            event.source != "ocr" or event in stable_ocr_events
            for event in candidate_events
        ):
            resolution = (
                "unresolved"
                if any(event.normalized_text != asr_norm for event in candidate_events)
                else "sources_agree"
            )
            if resolution == "unresolved":
                flags.append("UNRESOLVED")
                if unresolved:
                    provenance["unresolved_spans"] = unresolved
        elif ocr_events:
            local_ocr_conflicts = [
                (event, span)
                for event in ocr_events
                for span in find_local_replacement(asr_event.text, event.text)
                if span["eligible_context"]
            ]
            if local_ocr_conflicts:
                resolution = "unresolved"
                flags.append("UNRESOLVED")
                provenance["unresolved_spans"] = [
                    _span_record(event, span) for event, span in local_ocr_conflicts
                ]
            else:
                resolution = "asr_preserved_ocr_rejected"
                flags.append("OCR_REJECTED")

    provenance["selected_source_event_ids"] = selected_source_ids
    return TranscriptUnit(
        unit_id=f"unit-{index:06d}",
        start_ms=asr_event.start_ms,
        end_ms=asr_event.end_ms,
        canonical_text=normalize_text(canonical_text),
        asr_text=_presentation_text(actual_asr.text) if actual_asr is not None else None,
        ocr_text=_presentation_text(ocr.text) if ocr is not None else None,
        subtitle_text=_presentation_text(subtitle.text) if subtitle is not None else None,
        bilibili_ai_text=_presentation_text(ai_subtitle.text) if ai_subtitle is not None else None,
        resolution=resolution,
        flags=tuple(dict.fromkeys(flags)),
        provenance=provenance,
    )


def build_canonical_transcript(
    *,
    video_id: str,
    duration_ms: int,
    asr_events: Iterable[SourceTextEvent],
    subtitle_events: Iterable[SourceTextEvent] = (),
    ocr_events: Iterable[SourceTextEvent] = (),
    transcript_mode: TranscriptMode = "fused",
    source_status: dict[str, dict[str, Any]] | None = None,
    warnings: Iterable[str] = (),
    artifact_paths: dict[str, str] | None = None,
) -> TranscriptBuild:
    """Align optional sources to ASR anchors and perform only local fusion."""

    if transcript_mode not in {"asr-only", "fused"}:
        raise TranscriptFoundationError("transcript_mode must be asr-only or fused")
    if not video_id.strip() or duration_ms <= 0:
        raise TranscriptFoundationError("video_id and duration_ms must be valid")
    asr = sorted(asr_events, key=lambda event: (event.start_ms, event.end_ms, event.event_id))
    if not asr or any(event.source != "asr" for event in asr):
        raise TranscriptFoundationError("at least one ASR source event is required")
    subtitles = sorted(
        subtitle_events,
        key=lambda event: (event.start_ms, event.end_ms, event.event_id),
    )
    ocr = sorted(ocr_events, key=lambda event: (event.start_ms, event.end_ms, event.event_id))
    if any(event.source != "subtitle_track" for event in subtitles):
        raise TranscriptFoundationError("subtitle_events contains a non-subtitle source")
    if any(event.source != "ocr" for event in ocr):
        raise TranscriptFoundationError("ocr_events contains a non-OCR source")
    subtitle_assignments = _assign_optional_events(asr, subtitles)
    ocr_assignments = _assign_optional_events(asr, ocr)
    repeated_candidate_support = _repeated_ocr_candidate_support(asr, ocr_assignments)
    units: list[TranscriptUnit] = []
    aligned_optional_ids: set[str] = set()
    for index, asr_event in enumerate(asr, start=1):
        aligned_subtitles = subtitle_assignments[asr_event.event_id]
        aligned_ocr = ocr_assignments[asr_event.event_id]
        aligned_optional_ids.update(event.event_id for event in [*aligned_subtitles, *aligned_ocr])
        units.append(
            _build_unit(
                index,
                asr_event,
                aligned_subtitles,
                aligned_ocr,
                repeated_candidate_support,
            )
        )
    status_values = source_status or {
        "asr": {"status": "READY", "count": len(asr)},
        "subtitle_track": {
            "status": "READY" if subtitles else "ABSENT",
            "count": len(subtitles),
        },
        "ocr": {"status": "READY" if ocr else "OFF", "count": len(ocr)},
    }
    warning_list = list(dict.fromkeys(str(warning) for warning in warnings if str(warning)))
    if transcript_mode == "fused":
        if not subtitles and status_values.get("subtitle_track", {}).get("status") == "ABSENT":
            warning_list.append("SUBTITLE_ABSENT")
        if any(
            status_values.get(source, {}).get("status") in {"FAILED", "UNSUPPORTED", "UNSTABLE"}
            for source in ("subtitle_track", "ocr")
        ):
            warning_list.append("OPTIONAL_SOURCE_DEGRADED")
    unaligned_count = len(
        set(event.event_id for event in [*subtitles, *ocr]) - aligned_optional_ids
    )
    if unaligned_count:
        warning_list.append(f"UNALIGNED_OPTIONAL_EVENTS:{unaligned_count}")
    final_status: Literal["READY", "DEGRADED", "FAILED"] = (
        "DEGRADED" if warning_list and transcript_mode == "fused" else "READY"
    )
    manifest = TranscriptManifest(
        video_id=video_id,
        duration_ms=duration_ms,
        transcript_mode=transcript_mode,
        source_status=status_values,
        canonical_unit_count=len(units),
        status=final_status,
        warnings=tuple(dict.fromkeys(warning_list)),
        artifacts=artifact_paths or {},
    )
    return TranscriptBuild(
        source_events=tuple(sorted([*asr, *subtitles, *ocr], key=_event_sort_key)),
        canonical_units=tuple(units),
        manifest=manifest,
    )


def build_selected_transcript(
    *, video_id, duration_ms, selected_sources, source_events, source_status,
) -> TranscriptBuild:
    """Use the first available selected source as the timeline, retaining all evidence."""
    groups = {
        source: sorted(source_events.get(source, ()), key=_event_sort_key)
        for source in selected_sources
    }
    primary = next((source for source in selected_sources if groups[source]), None)
    if primary is None:
        raise TranscriptFoundationError("所选来源均未取得可用文字稿；没有调用未选择的 ASR。")
    anchors = groups[primary]
    optional = [
        event for source in selected_sources if source != primary for event in groups[source]
    ]
    assignments = _assign_optional_events(anchors, optional)
    units = tuple(
        _build_unit(index, anchor, assignments[anchor.event_id], ())
        for index, anchor in enumerate(anchors, start=1)
    )
    warnings = [
        f"{source}:{source_status[source]['status']}"
        for source in selected_sources if source_status[source]["status"] != "READY"
    ]
    if primary != selected_sources[0]:
        warnings.append(f"PRIMARY_UNAVAILABLE:using_{primary}")
    aligned = {event.event_id for events in assignments.values() for event in events}
    if len(aligned) != len(optional):
        warnings.append(f"UNALIGNED_OPTIONAL_EVENTS:{len(optional) - len(aligned)}")
    return TranscriptBuild(
        source_events=tuple(sorted(
            (event for events in groups.values() for event in events), key=_event_sort_key,
        )),
        canonical_units=units,
        manifest=TranscriptManifest(
            video_id=video_id, duration_ms=duration_ms, transcript_mode="selected",
            source_status=source_status, canonical_unit_count=len(units),
            status="DEGRADED" if warnings else "READY", warnings=tuple(warnings),
            selected_sources=tuple(selected_sources), primary_source=primary,
        ),
    )


def _event_sort_key(event: SourceTextEvent) -> tuple[int, int, int, str]:
    source_order = {"asr": 0, "subtitle_track": 1, "ocr": 2, "bilibili_ai": 3}
    return event.start_ms, event.end_ms, source_order[event.source], event.event_id


def _jsonl_rows(items: Iterable[BaseModel]) -> str:
    return "".join(
        json.dumps(item.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in items
    )


def _jsonl_objects(items: Iterable[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in items
    )


def frame_candidate_rows(events: Iterable[SourceTextEvent]) -> list[dict[str, Any]]:
    """Return the retained local OCR candidates for a debug/review JSONL."""

    rows: list[dict[str, Any]] = []
    for event in events:
        if event.source != "ocr":
            continue
        candidates = event.provenance.get("frame_candidates")
        if isinstance(candidates, list):
            rows.extend(
                candidate
                for candidate in candidates
                if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
            )
    return sorted(rows, key=lambda row: (row["timestamp_ms"], row["candidate_id"]))


def _accepted_multi_character_replacements(
    build: TranscriptBuild,
) -> list[dict[str, Any]]:
    events = {event.event_id: event for event in build.source_events}
    rows: list[dict[str, Any]] = []
    for unit in build.canonical_units:
        accepted_spans = unit.provenance.get("accepted_spans")
        if not isinstance(accepted_spans, list):
            continue
        for span in accepted_spans:
            if not isinstance(span, dict):
                continue
            event_id = span.get("event_id")
            event = events.get(event_id) if isinstance(event_id, str) else None
            if event is None:
                continue
            asr_span = span.get("asr_span")
            candidate_span = span.get("candidate_span")
            if not isinstance(asr_span, str) or not isinstance(candidate_span, str):
                continue
            if max(len(normalize_text(asr_span)), len(normalize_text(candidate_span))) <= 1:
                continue
            candidate_ids = _consensus_candidate_ids(event)
            support = event.provenance.get("consensus_support")
            rows.append(
                {
                    "unit_id": unit.unit_id,
                    "unit_start_ms": unit.start_ms,
                    "unit_end_ms": unit.end_ms,
                    "asr_start": span.get("asr_start"),
                    "asr_end": span.get("asr_end"),
                    "asr_span": asr_span,
                    "accepted_source_span": candidate_span,
                    "source_event_id": event.event_id,
                    "source": event.source,
                    "candidate_ids": candidate_ids,
                    "consensus_support": support if isinstance(support, int) else None,
                    "representative_candidate_id": event.provenance.get(
                        "representative_candidate_id"
                    ),
                    "observed_variants": event.provenance.get("observed_variants", []),
                }
            )
    return sorted(
        rows,
        key=lambda row: (
            row["unit_start_ms"],
            row["asr_start"] if isinstance(row["asr_start"], int) else -1,
            row["source_event_id"],
        ),
    )


def _atomic_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.partial")
    temporary_path.write_text(contents, encoding="utf-8")
    temporary_path.replace(path)


def write_transcript_artifacts(
    build: TranscriptBuild,
    output_root: Path,
    *,
    write_ocr_events: bool = False,
) -> dict[str, Path]:
    """Write the canonical foundation files and minimally extend ingest manifest."""

    output_root.mkdir(parents=True, exist_ok=True)
    paths = {
        "source_text_events": output_root / "source-text-events.jsonl",
        "canonical_transcript": output_root / "canonical-transcript.jsonl",
        "transcript_manifest": output_root / "transcript-manifest.json",
        "accepted_multi_character_replacements": output_root
        / "accepted-multi-character-replacements.jsonl",
    }
    if write_ocr_events:
        paths["subtitle_ocr_events"] = output_root / "subtitle-ocr-events.jsonl"
        paths["subtitle_ocr_frame_candidates"] = output_root / "subtitle-ocr-frame-candidates.jsonl"
    _atomic_text(paths["source_text_events"], _jsonl_rows(build.source_events))
    _atomic_text(paths["canonical_transcript"], _jsonl_rows(build.canonical_units))
    if write_ocr_events:
        ocr_rows = [event for event in build.source_events if event.source == "ocr"]
        _atomic_text(paths["subtitle_ocr_events"], _jsonl_rows(ocr_rows))
        _atomic_text(
            paths["subtitle_ocr_frame_candidates"],
            _jsonl_objects(frame_candidate_rows(ocr_rows)),
        )
    _atomic_text(
        paths["accepted_multi_character_replacements"],
        _jsonl_objects(_accepted_multi_character_replacements(build)),
    )
    artifact_names = {key: str(path.name) for key, path in paths.items()}
    manifest = build.manifest.model_copy(update={"artifacts": artifact_names})
    _atomic_text(
        paths["transcript_manifest"],
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    )
    return paths


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptFoundationError(f"invalid JSON input: {path}") from exc
    if not isinstance(payload, dict):
        raise TranscriptFoundationError(f"JSON input must be an object: {path}")
    return payload


def parse_subtitle_or_event_file(path: Path) -> list[SourceTextEvent]:
    if path.suffix.lower() in SUPPORTED_SUBTITLE_SUFFIXES:
        return parse_subtitle_file(path)
    return load_event_jsonl(path, expected_source="subtitle_track")


__all__ = [
    "ALIGNMENT_JITTER_MS",
    "AutoRoiResult",
    "FUSION_VERSION",
    "NORMALIZER_VERSION",
    "OCRBackend",
    "OcrResult",
    "OcrDetection",
    "OcrError",
    "RapidOcrBackend",
    "SampledFrame",
    "SourceTextEvent",
    "SubtitleOcrEvent",
    "Stability",
    "SubtitleDiscovery",
    "SubtitleParseError",
    "TranscriptBuild",
    "TranscriptFoundationError",
    "TranscriptManifest",
    "TranscriptUnit",
    "asr_events_from_payload",
    "build_canonical_transcript",
    "detect_auto_roi",
    "discover_subtitle_source",
    "extract_subtitle_ocr_events",
    "frame_candidate_rows",
    "filter_changed_frames",
    "find_local_replacement",
    "load_event_jsonl",
    "normalize_text",
    "parse_roi",
    "parse_subtitle_file",
    "parse_subtitle_or_event_file",
    "read_json_object",
    "roi_pixel_bounds",
    "sample_video_frames",
    "select_subtitle_file",
    "write_transcript_artifacts",
]


# The task-card name is retained as a discoverable alias while the shared
# source-event contract keeps one validation and serialization path.
SubtitleOcrEvent = SourceTextEvent
