from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_report_agent.transcript_foundation import (
    ALIGNMENT_JITTER_MS,
    OcrDetection,
    OcrResult,
    RapidOcrBackend,
    SampledFrame,
    SourceTextEvent,
    Stability,
    SubtitleOcrEvent,
    _assign_optional_events,
    _consensus_for_group,
    _merge_ocr_samples,
    _OcrSample,
    build_canonical_transcript,
    detect_auto_roi,
    discover_subtitle_source,
    extract_subtitle_ocr_events,
    filter_changed_frames,
    normalize_text,
    parse_roi,
    parse_subtitle_file,
    roi_pixel_bounds,
)


def _event(
    source: str,
    event_id: str,
    text: str,
    start_ms: int = 1_000,
    end_ms: int = 3_000,
    *,
    stability: Stability | None = None,
    confidence: float | None = None,
    provenance: dict[str, object] | None = None,
) -> SourceTextEvent:
    return SourceTextEvent(
        event_id=event_id,
        source=source,  # type: ignore[arg-type]
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        normalized_text=normalize_text(text),
        confidence=confidence,
        stability=stability or Stability(sample_count=1, distinct_frame_count=1),
        provenance=({"asr_ordinal": 0} if source == "asr" else (provenance or {})),
    )


def test_roi_validation_and_containing_pixel_crop() -> None:
    assert parse_roi("0.05,0.72,0.95,0.98") == (0.05, 0.72, 0.95, 0.98)
    assert roi_pixel_bounds(1920, 1080, (0.05, 0.72, 0.95, 0.98)) == (
        96,
        777,
        1824,
        1059,
    )
    assert normalize_text("中文  文字") == "中文文字"
    assert SubtitleOcrEvent is SourceTextEvent


def test_visual_change_filter_keeps_only_perceptually_changed_frames(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    frames: list[SampledFrame] = []
    for index, value in enumerate((0, 0, 255, 255)):
        path = tmp_path / f"frame-{index:06d}.jpg"
        image = value * (cv2.UMat(32, 32, cv2.CV_8UC1).get() + 1)
        cv2.imwrite(str(path), image)
        frames.append(SampledFrame(index * 500, path))

    retained = filter_changed_frames(frames, (0.0, 0.0, 1.0, 1.0))

    assert [frame.timestamp_ms for frame in retained] == [0, 1_000]


def test_explicit_ocr_merges_repeated_samples_and_retains_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    cv2 = pytest.importorskip("cv2")
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"local placeholder")

    def fake_sample_video_frames(*args, **kwargs):
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, value in enumerate((0, 80, 160, 240)):
            path = output_dir / f"frame-{index:06d}.jpg"
            image = value * (cv2.UMat(48, 96, cv2.CV_8UC1).get() + 1)
            cv2.imwrite(str(path), image)
            frames.append(SampledFrame(index * 500, path))
        return frames

    class FakeOcr:
        provider = "rapidocr"
        model = "PP-OCRv6-small-onnxruntime"
        def recognize(self, image_path: Path):
            return OcrResult((
                OcrDetection(
                    text="我们使用 RLinf 进行训练",
                    confidence=0.93,
                ),
            ), self.provider, self.model)

    monkeypatch.setattr(
        "video_report_agent.transcript_foundation.sample_video_frames",
        fake_sample_video_frames,
    )
    events = extract_subtitle_ocr_events(
        video_path,
        "0.05,0.72,0.95,0.98",
        sample_fps=2,
        duration_ms=2_000,
        adapter=FakeOcr(),
    )

    assert len(events) == 1
    assert events[0].event_id == "ocr-000001"
    assert events[0].stability.sample_count >= 3
    assert events[0].stability.distinct_frame_count >= 3
    assert events[0].provenance["frame_timestamps_ms"][0] == 0
    assert events[0].provenance["roi"] == [0.05, 0.72, 0.95, 0.98]
    assert events[0].provenance["candidate_ids"] == [
        "ocr-frame-000001",
        "ocr-frame-000002",
        "ocr-frame-000003",
        "ocr-frame-000004",
    ]
    assert events[0].provenance["consensus_candidate_ids"] == events[0].provenance["candidate_ids"]
    assert events[0].provenance["representative_candidate_id"] == "ocr-frame-000001"
    assert events[0].provenance["consensus_support"] == 4
    assert events[0].provenance["consensus_eligible"] is True
    assert len(events[0].provenance["frame_candidates"]) == 4
    assert events[0].provenance["frame_candidates"][0] == {
        "candidate_id": "ocr-frame-000001",
        "timestamp_ms": 0,
        "observed_text": "我们使用 RLinf 进行训练",
        "normalized_text": "我们使用 RLinf 进行训练",
        "confidence": 0.93,
        "roi": [0.05, 0.72, 0.95, 0.98],
        "frame_id": "frame-000000.jpg",
        "sample_id": "frame-000000",
    }
    assert events[0].provenance["processed_start_ms"] == 0
    assert events[0].provenance["processed_end_ms"] == 2_000
    assert events[0].provenance["video_duration_ms"] == 2_000
    assert events[0].provenance["coverage_ratio"] == 1.0
    assert events[0].provenance["coverage_status"] == "FULL"
    assert events[0].provenance["ocr_engine"] == "onnxruntime"
    assert events[0].provenance["ocr_detection_model"] == "PP-OCRv6-small"
    assert events[0].provenance["ocr_recognition_model"] == "PP-OCRv6-small"
    assert events[0].end_ms == events[0].provenance["frame_timestamps_ms"][-1] + 500


def test_rapidocr_row_shape_is_coerced_without_combining_scores() -> None:
    from video_report_agent.transcript_foundation import normalize_ocr_result

    class FakeRapidOcr:
        def recognize(self, image_path: Path):
            return (
                [
                    [
                        [[12, 4], [80, 4], [80, 24], [12, 24]],
                        ("RLinf", 0.91),
                    ]
                ],
                [0.004],
            )

    detections = normalize_ocr_result(FakeRapidOcr().recognize(Path("frame.jpg")))

    assert detections == [
        OcrDetection(
            text="RLinf",
            confidence=0.91,
            points=((12.0, 4.0), (80.0, 4.0), (80.0, 24.0), (12.0, 24.0)),
        )
    ]


def test_unified_rapidocr_output_maps_to_existing_detection_boundary() -> None:
    from video_report_agent.transcript_foundation import normalize_ocr_result

    np = pytest.importorskip("numpy")
    class FakeRapidOcr:
        def recognize(self, image_path: Path):
            return SimpleNamespace(
                boxes=np.asarray([[[12, 4], [80, 4], [80, 24], [12, 24]]]),
                txts=("星象房",),
                scores=(0.94,),
            )

    detections = normalize_ocr_result(FakeRapidOcr().recognize(Path("frame.jpg")))

    assert detections == [
        OcrDetection(
            text="星象房",
            confidence=0.94,
            points=((12.0, 4.0), (80.0, 4.0), (80.0, 24.0), (12.0, 24.0)),
        )
    ]


def test_unified_rapidocr_empty_output_maps_to_no_detections() -> None:
    from video_report_agent.transcript_foundation import normalize_ocr_result

    class FakeRapidOcr:
        def recognize(self, image_path: Path):
            return SimpleNamespace(boxes=None, txts=None, scores=None)

    assert normalize_ocr_result(FakeRapidOcr().recognize(Path("frame.jpg"))) == []


def test_rapidocr_adapter_requests_ppocrv6_small_onnxruntime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rapidocr = pytest.importorskip("rapidocr")
    pytest.importorskip("cv2")  # RapidOCR lazily imports its image-processing engine.

    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, *, params):
            captured.update(params)

        def __call__(self, image_path: str):
            return SimpleNamespace(boxes=None, txts=None, scores=None)

    monkeypatch.setattr(rapidocr, "RapidOCR", FakeEngine)
    adapter = RapidOcrBackend()

    assert captured == {
        "Det.engine_type": rapidocr.EngineType.ONNXRUNTIME,
        "Det.lang_type": rapidocr.LangDet.CH,
        "Det.model_type": rapidocr.ModelType.SMALL,
        "Det.ocr_version": rapidocr.OCRVersion.PPOCRV6,
        "Rec.engine_type": rapidocr.EngineType.ONNXRUNTIME,
        "Rec.lang_type": rapidocr.LangRec.CH,
        "Rec.model_type": rapidocr.ModelType.SMALL,
        "Rec.ocr_version": rapidocr.OCRVersion.PPOCRV6,
    }
    assert adapter.recognize(Path("frame.jpg")).detections == ()


def test_auto_roi_selects_a_recurring_lower_sentence_band(tmp_path: Path, monkeypatch) -> None:
    cv2 = pytest.importorskip("cv2")
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"local placeholder")

    def fake_sample_video_frames(*args, **kwargs):
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, value in enumerate((0, 80, 160)):
            path = output_dir / f"frame-{index:06d}.jpg"
            image = value * (cv2.UMat(100, 200, cv2.CV_8UC1).get() + 1)
            cv2.imwrite(str(path), image)
            frames.append(SampledFrame(index * 500, path))
        return frames

    class FakeOcr:
        provider = "rapidocr"
        model = "PP-OCRv6-small-onnxruntime"
        def recognize(self, image_path: Path):
            index = int(image_path.stem.split("-")[-1])
            return OcrResult((
                OcrDetection(
                    text=f"字幕内容{index}",
                    confidence=0.9,
                    points=((20, 78), (180, 78), (180, 90), (20, 90)),
                ),
            ), self.provider, self.model)

    monkeypatch.setattr(
        "video_report_agent.transcript_foundation.sample_video_frames",
        fake_sample_video_frames,
    )
    result = detect_auto_roi(video_path, adapter=FakeOcr())

    assert result.status == "READY"
    assert result.roi is not None
    assert result.roi[1] > 0.5


def test_subtitle_discovery_prefers_chinese_sidecar_and_marks_image_track_unsupported(
    tmp_path: Path,
) -> None:
    media_path = tmp_path / "source.mp4"
    media_path.write_bytes(b"local placeholder")
    english = tmp_path / "source.en.srt"
    chinese = tmp_path / "source.zh.srt"
    english.write_text("1\n00:00:01,000 --> 00:00:02,000\nEnglish\n", encoding="utf-8")
    chinese.write_text("1\n00:00:01,000 --> 00:00:02,000\n中文\n", encoding="utf-8")

    selected = discover_subtitle_source(media_path)

    assert selected.status == "AVAILABLE"
    assert selected.path == chinese.resolve()

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"index": 2, "codec_name": "hdmv_pgs_subtitle"}]}),
        )

    english.unlink()
    chinese.unlink()
    unsupported = discover_subtitle_source(media_path, artifact_root=tmp_path, runner=runner)

    assert unsupported.status == "UNSUPPORTED"
    assert unsupported.warning == "SUBTITLE_UNSUPPORTED"


def test_subtitle_parsing_supports_srt_vtt_and_ass(tmp_path: Path) -> None:
    pytest.importorskip("pysubs2")
    samples = {
        ".srt": "1\n00:00:01,000 --> 00:00:03,000\n中文 RLinf\n",
        ".vtt": "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n中文 VTT\n",
        ".ass": (
            "[Script Info]\nScriptType: v4.00+\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, TertiaryColour, BackColour, Bold, Italic, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
            "Style: Default,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00000000,"
            "0,0,1,2,0,2,10,10,10,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,中文 ASS\n"
        ),
    }
    for suffix, contents in samples.items():
        path = tmp_path / f"caption{suffix}"
        path.write_text(contents, encoding="utf-8")
        events = parse_subtitle_file(path)
        assert len(events) == 1
        assert events[0].source == "subtitle_track"
        assert events[0].start_ms == 1_000
        assert events[0].provenance["format"] == suffix[1:]


def test_local_fusion_corrects_one_span_and_projects_unit_provenance() -> None:
    asr = _event("asr", "asr-000000", "我们使用 Rlinf 进行训练")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "我们使用 RLinf 进行训练",
        stability=Stability(sample_count=4, distinct_frame_count=4),
    )

    result = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        ocr_events=[ocr],
    )

    unit = result.canonical_units[0]
    assert unit.canonical_text == "我们使用 RLinf 进行训练"
    assert unit.resolution == "ocr_corrected_asr"
    assert unit.provenance["selected_source_event_ids"] == ["ocr-000001"]


def test_adjacent_mixed_ocr_conflict_stays_unresolved() -> None:
    asr = _event("asr", "asr-000000", "并且由于礼店的商店是非常非常好的")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "并且由干里店的商店是非常非常好的",
        stability=Stability(sample_count=4, distinct_frame_count=4),
        provenance={
            "roi_mode": "explicit",
            "consensus_eligible": False,
            "consensus_unique": False,
            "consensus_support": 1,
        },
    )

    unit = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        ocr_events=[ocr],
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags


def test_single_frame_explicit_roi_high_confidence_cannot_correct_short_span() -> None:
    asr = _event("asr", "asr-000000", "我们今天跑办程训练任务")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "我们今天跑半程训练任务",
        confidence=0.95,
        provenance={"roi_mode": "explicit"},
    )

    unit = build_canonical_transcript(
        video_id="demo", duration_ms=10_000, asr_events=[asr], ocr_events=[ocr]
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags


def test_single_frame_explicit_roi_below_threshold_is_rejected() -> None:
    asr = _event("asr", "asr-000000", "我们今天跑办程训练任务")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "我们今天跑半程训练任务",
        confidence=0.89,
        provenance={"roi_mode": "explicit"},
    )

    unit = build_canonical_transcript(
        video_id="demo", duration_ms=10_000, asr_events=[asr], ocr_events=[ocr]
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags


def test_single_frame_auto_roi_remains_rejected_at_high_confidence() -> None:
    asr = _event("asr", "asr-000000", "我们今天跑办程训练任务")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "我们今天跑半程训练任务",
        confidence=0.99,
        provenance={"roi_mode": "auto"},
    )

    unit = build_canonical_transcript(
        video_id="demo", duration_ms=10_000, asr_events=[asr], ocr_events=[ocr]
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags


def test_ocr_consensus_failure_keeps_mixed_candidate_unresolved() -> None:
    asr = _event("asr", "asr-000000", "由于礼店今天使用R英孚进行训练")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "由干里店今天使用RLinf进行训练",
        confidence=0.97,
        stability=Stability(sample_count=3, distinct_frame_count=3),
        provenance={
            "roi_mode": "explicit",
            "consensus_eligible": False,
            "consensus_unique": False,
            "consensus_support": 1,
        },
    )

    unit = build_canonical_transcript(
        video_id="demo", duration_ms=10_000, asr_events=[asr], ocr_events=[ocr]
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags
    assert "accepted_spans" not in unit.provenance
    assert {span["candidate_span"] for span in unit.provenance["unresolved_spans"]} == {
        "干里",
        "Linf",
    }


def test_cross_frame_consensus_accepts_short_multicharacter_replacements() -> None:
    consensus_provenance = {
        "roi_mode": "explicit",
        "candidate_ids": ["ocr-frame-000001", "ocr-frame-000002", "ocr-frame-000003"],
        "consensus_candidate_ids": [
            "ocr-frame-000001",
            "ocr-frame-000002",
            "ocr-frame-000003",
        ],
        "representative_candidate_id": "ocr-frame-000002",
        "consensus_support": 3,
        "consensus_unique": True,
        "consensus_eligible": True,
        "observed_variants": ["这里是星象房月亮"],
    }
    cases = (
        ("这里是新相房月亮", "这里是星象房月亮"),
        ("数量是甲个", "数量是128个"),
        ("本次使用Rxx版本", "本次使用RLinf版本"),
    )

    for index, (asr_text, ocr_text) in enumerate(cases, start=1):
        asr = _event("asr", f"asr-{index:06d}", asr_text)
        ocr = _event(
            "ocr",
            f"ocr-{index:06d}",
            ocr_text,
            stability=Stability(sample_count=3, distinct_frame_count=3),
            provenance=consensus_provenance,
        )
        unit = build_canonical_transcript(
            video_id=f"demo-{index}",
            duration_ms=10_000,
            asr_events=[asr],
            ocr_events=[ocr],
        ).canonical_units[0]

        assert unit.canonical_text == normalize_text(ocr_text)
        assert unit.resolution == "ocr_corrected_asr"
        assert all(
            1 <= len(normalize_text(span["asr_span"])) <= 8
            and 1 <= len(normalize_text(span["candidate_span"])) <= 8
            for span in unit.provenance["accepted_spans"]
        )


def test_frame_consensus_rejects_adjacent_unsafe_changes() -> None:
    asr = _event("asr", "asr-000000", "并且由于礼店的商店是非常非常好的")
    frame_texts = (
        "并且由干里店的商店是非常非常好的",
        "并且由干里店的商店是非常非常好的",
        "并且由干里一店的商店是非常非常好的",
    )
    candidate_ids = [f"ocr-frame-{index:06d}" for index in range(1, 4)]
    ocr = _event(
        "ocr",
        "ocr-000001",
        frame_texts[0],
        confidence=0.95,
        stability=Stability(sample_count=3, distinct_frame_count=3),
        provenance={
            "candidate_ids": candidate_ids,
            "consensus_candidate_ids": candidate_ids,
            "consensus_support": 3,
            "consensus_unique": True,
            "consensus_eligible": True,
            "frame_candidates": [
                {
                    "candidate_id": candidate_id,
                    "observed_text": text,
                }
                for candidate_id, text in zip(candidate_ids, frame_texts, strict=True)
            ],
        },
    )

    unit = build_canonical_transcript(
        video_id="demo", duration_ms=10_000, asr_events=[asr], ocr_events=[ocr]
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags
    assert "accepted_spans" not in unit.provenance


def test_frame_consensus_allows_distant_unsafe_span_but_not_target_pollution() -> None:
    asr = _event("asr", "asr-000000", "之前留的一地钱就是打算踩踩陷阱的")
    frame_texts = (
        "的一地钱就是打算踩踩献祭的",
        "之前留的一地钱就是打算踩踩献祭的",
        "之前留的一地钱就是打算踩踩献祭的",
    )
    candidate_ids = [f"ocr-frame-{index:06d}" for index in range(1, 4)]
    provenance = {
        "candidate_ids": candidate_ids,
        "consensus_candidate_ids": candidate_ids,
        "consensus_support": 3,
        "consensus_unique": True,
        "consensus_eligible": True,
        "frame_candidates": [
            {
                "candidate_id": candidate_id,
                "observed_text": text,
            }
            for candidate_id, text in zip(candidate_ids, frame_texts, strict=True)
        ],
    }
    ocr = _event(
        "ocr",
        "ocr-000001",
        frame_texts[1],
        confidence=0.95,
        stability=Stability(sample_count=3, distinct_frame_count=3),
        provenance=provenance,
    )
    repeated_ocr = _event(
        "ocr",
        "ocr-000002",
        frame_texts[1],
        confidence=0.95,
        stability=Stability(sample_count=3, distinct_frame_count=3),
        provenance={
            **provenance,
            "candidate_ids": [f"ocr-frame-{index:06d}" for index in range(4, 7)],
            "consensus_candidate_ids": [f"ocr-frame-{index:06d}" for index in range(4, 7)],
            "frame_candidates": [
                {
                    "candidate_id": f"ocr-frame-{index:06d}",
                    "observed_text": frame_texts[1],
                }
                for index in range(4, 7)
            ],
        },
    )

    unit = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        ocr_events=[ocr, repeated_ocr],
    ).canonical_units[0]

    assert "献祭" in unit.canonical_text
    assert unit.resolution == "ocr_corrected_asr"
    assert any(span["candidate_span"] == "献祭" for span in unit.provenance["accepted_spans"])


def test_unrepeated_cjk_multicharacter_ocr_stays_unresolved() -> None:
    asr = _event("asr", "asr-000000", "并且由于礼店的商店是非常非常好的")
    candidate_ids = [f"ocr-frame-{index:06d}" for index in range(1, 5)]
    ocr = _event(
        "ocr",
        "ocr-000001",
        "并且由干里店的商店是非常非常好的",
        confidence=0.95,
        stability=Stability(sample_count=4, distinct_frame_count=4),
        provenance={
            "candidate_ids": candidate_ids,
            "consensus_candidate_ids": candidate_ids,
            "consensus_support": 4,
            "consensus_unique": True,
            "consensus_eligible": True,
            "frame_candidates": [
                {
                    "candidate_id": candidate_id,
                    "observed_text": ocr_text,
                }
                for candidate_id, ocr_text in zip(
                    candidate_ids,
                    [
                        "并且由干里店的商店是非常非常好的",
                        "并且由干里店的商店是非常非常好的",
                        "并且由干里店的商店是非常非常好的",
                        "并且由干里店的商店是非常非常好的",
                    ],
                    strict=True,
                )
            ],
        },
    )

    unit = build_canonical_transcript(
        video_id="demo", duration_ms=10_000, asr_events=[asr], ocr_events=[ocr]
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags


def test_consensus_medoid_is_an_observed_candidate_and_ties_are_ineligible() -> None:
    samples = [
        _OcrSample(
            0, Path("frame-a.jpg"), "前后一二三四五六", normalize_text("前后一二三四五六"), 0.9
        ),
        _OcrSample(
            500,
            Path("frame-b.jpg"),
            "前后一二三四五七",
            normalize_text("前后一二三四五七"),
            0.95,
        ),
        _OcrSample(
            1_000,
            Path("frame-c.jpg"),
            "前后一二三四五七",
            normalize_text("前后一二三四五七"),
            0.94,
        ),
    ]
    representative, cluster, support, unique = _consensus_for_group(samples)

    assert representative.text == "前后一二三四五七"
    assert representative in cluster
    assert support == 3
    assert unique is True

    tie_samples = [
        _OcrSample(0, Path("tie-a.jpg"), "前文甲甲甲", normalize_text("前文甲甲甲"), 0.9),
        _OcrSample(500, Path("tie-b.jpg"), "前文甲甲甲", normalize_text("前文甲甲甲"), 0.9),
        _OcrSample(1_000, Path("tie-c.jpg"), "后文乙乙乙", normalize_text("后文乙乙乙"), 0.9),
        _OcrSample(1_500, Path("tie-d.jpg"), "后文乙乙乙", normalize_text("后文乙乙乙"), 0.9),
    ]
    _, _, tie_support, tie_unique = _consensus_for_group(tie_samples)

    assert tie_support == 2
    assert tie_unique is False


def test_ocr_merge_retains_candidate_ids_and_observed_consensus_provenance() -> None:
    events = _merge_ocr_samples(
        [
            _OcrSample(0, Path("frame-1.jpg"), "字幕甲", "字幕甲", 0.8),
            _OcrSample(500, Path("frame-2.jpg"), "字幕甲", "字幕甲", 0.9),
        ],
        roi=(0.1, 0.7, 0.9, 0.98),
        sample_fps=2,
        duration_ms=2_000,
    )

    assert events[0].provenance["candidate_ids"] == [
        "ocr-frame-000001",
        "ocr-frame-000002",
    ]
    assert events[0].provenance["observed_variants"] == ["字幕甲"]
    assert events[0].provenance["consensus_support"] == 2
    assert events[0].provenance["representative_candidate_id"] == "ocr-frame-000002"


def test_neighboring_unit_alignment_is_bounded_to_one_side_each() -> None:
    asr_events = [
        _event("asr", "asr-000000", "前", 0, 1_000),
        _event("asr", "asr-000001", "当前", 2_000, 3_000),
        _event("asr", "asr-000002", "后", 4_000, 5_000),
        _event("asr", "asr-000003", "远", 6_000, 7_000),
    ]
    optional_events = [
        _event("subtitle_track", "subtitle-000001", "前", 1_200, 1_600),
        _event("subtitle_track", "subtitle-000002", "当前", 2_200, 2_400),
        _event("subtitle_track", "subtitle-000003", "后", 5_200, 5_400),
        _event("subtitle_track", "subtitle-000004", "远", 9_000, 9_500),
    ]

    assigned = _assign_optional_events(asr_events, optional_events)

    assert [event.event_id for event in assigned["asr-000000"]] == ["subtitle-000001"]
    assert [event.event_id for event in assigned["asr-000001"]] == ["subtitle-000002"]
    assert [event.event_id for event in assigned["asr-000002"]] == ["subtitle-000003"]
    assert assigned["asr-000003"] == []


def test_ocr_insertions_and_deletions_are_rejected() -> None:
    stable = Stability(sample_count=3, distinct_frame_count=3)
    inserted = build_canonical_transcript(
        video_id="insert",
        duration_ms=10_000,
        asr_events=[_event("asr", "asr-000000", "我们使用 Rinf 进行训练")],
        ocr_events=[
            _event(
                "ocr",
                "ocr-000001",
                "我们使用 RLinf 进行训练",
                confidence=0.96,
                stability=stable,
                provenance={"roi_mode": "explicit"},
            )
        ],
    ).canonical_units[0]
    deleted = build_canonical_transcript(
        video_id="delete",
        duration_ms=10_000,
        asr_events=[_event("asr", "asr-000000", "所以钱得花在刀刃上")],
        ocr_events=[
            _event(
                "ocr",
                "ocr-000001",
                "所以钱得花在刀上",
                confidence=0.96,
                stability=stable,
                provenance={"roi_mode": "explicit"},
            )
        ],
    ).canonical_units[0]

    assert inserted.canonical_text == "我们使用 Rinf 进行训练"
    assert inserted.resolution == "unresolved"
    assert "UNRESOLVED" in inserted.flags
    assert deleted.canonical_text == "所以钱得花在刀刃上"
    assert deleted.resolution == "unresolved"
    assert "UNRESOLVED" in deleted.flags


def test_agreeing_subtitle_and_ocr_are_reported_without_rewriting_asr() -> None:
    asr = _event("asr", "asr-000000", "我们使用 RLinf 进行训练")
    subtitle = _event("subtitle_track", "subtitle-000001", "我们使用 RLinf 进行训练")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "我们使用 RLinf 进行训练",
        stability=Stability(sample_count=2, distinct_frame_count=2),
    )

    unit = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        subtitle_events=[subtitle],
        ocr_events=[ocr],
    ).canonical_units[0]

    assert unit.resolution == "sources_agree"
    assert unit.canonical_text == unit.asr_text


def test_unstable_ocr_does_not_pollute_correct_asr() -> None:
    asr = _event("asr", "asr-000000", "正确的本地名称已经识别")
    ocr = _event("ocr", "ocr-000001", "完全错误的文字")

    unit = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        ocr_events=[ocr],
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "asr_preserved_ocr_rejected"
    assert "OCR_REJECTED" in unit.flags


def test_reliable_subtitle_correction_rejects_ocr_outlier() -> None:
    asr = _event("asr", "asr-000000", "我们使用R英孚进行训练")
    subtitle = _event("subtitle_track", "subtitle-000001", "我们使用 RLinf 进行训练")
    ocr = _event(
        "ocr",
        "ocr-000001",
        "完全错误的文字",
        stability=Stability(sample_count=3, distinct_frame_count=3),
    )

    unit = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        subtitle_events=[subtitle],
        ocr_events=[ocr],
    ).canonical_units[0]

    assert unit.resolution == "subtitle_corrected_asr"
    assert unit.canonical_text == "我们使用 RLinf 进行训练"
    assert unit.provenance["rejected_event_ids"] == ["ocr-000001"]
    assert "OCR_REJECTED" in unit.flags


def test_whole_sentence_conflict_stays_provisional_and_unresolved() -> None:
    asr = _event("asr", "asr-000000", "今天我们讨论训练系统")
    subtitle = _event("subtitle_track", "subtitle-000001", "明天我们讨论报告渲染")

    unit = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        subtitle_events=[subtitle],
    ).canonical_units[0]

    assert unit.canonical_text == normalize_text(asr.text)
    assert unit.resolution == "unresolved"
    assert "UNRESOLVED" in unit.flags
    assert unit.provenance["candidate_event_ids"] == ["subtitle-000001"]


def test_alignment_uses_small_jitter_but_keeps_distant_candidate_unaligned() -> None:
    asr = _event("asr", "asr-000000", "本地字幕内容", 0, 1_000)
    near = _event("subtitle_track", "subtitle-000001", "本地字幕内容", 1_400, 2_000)
    far = _event("subtitle_track", "subtitle-000002", "本地字幕内容", 3_000, 4_000)
    result = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=[asr],
        subtitle_events=[near, far],
    )

    assert ALIGNMENT_JITTER_MS == 1_500
    assert result.canonical_units[0].subtitle_text == "本地字幕内容"
    assert result.manifest.warnings == ("UNALIGNED_OPTIONAL_EVENTS:1",)


def test_source_event_words_and_asr_only_manifest_are_preserved() -> None:
    payload = {
        "segments": [{"ordinal": 3, "start_ms": 0, "end_ms": 1_000, "text": "ASR 文本",
                      "words": [{"word": "ASR"}]}],
        "raw_result": {"segments": [{}, {}, {}, {"words": [{"word": "ASR"}]}]},
    }
    from video_report_agent.transcript_foundation import asr_events_from_payload

    events = asr_events_from_payload(payload)
    result = build_canonical_transcript(
        video_id="demo",
        duration_ms=10_000,
        asr_events=events,
        transcript_mode="asr-only",
    )

    assert events[0].provenance["word_timestamps"] == [{"word": "ASR"}]
    assert result.manifest.status == "READY"
    assert result.manifest.transcript_mode == "asr-only"
