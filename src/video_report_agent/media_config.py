"""Resolve backend first, then its model; persist only non-secret settings."""

import os
from urllib.parse import urlsplit

from .asr import MlxWhisperBackend

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
OCR_MODEL = "PP-OCRv6-small-onnxruntime"
PARAFORMER_PARAMETERS = {
    "channel_id": [0],
    "language_hints": ["zh", "en"],
    "timestamp_alignment_enabled": True,
    "disfluency_removal_enabled": False,
}


FUN_ASR_MODELS = {
    "fun-asr",
    "fun-asr-2025-08-25",
    "fun-asr-2025-11-07",
    "fun-asr-mtl",
    "fun-asr-mtl-2025-08-25",
}


def cloud_asr_parameters(model):
    if model == "paraformer-v2":
        return dict(PARAFORMER_PARAMETERS)
    if model in FUN_ASR_MODELS:
        return {"channel_id": [0], "language_hints": ["zh"]}
    raise ValueError(
        f"Unsupported file ASR model: {model}; use paraformer-v2 "
        "or a supported Fun-ASR model (not realtime or Qwen-ASR)."
    )


def resolve_media_config(
    asr_backend=None,
    asr_model=None,
    ocr_backend=None,
    *,
    require_credentials=True,
):
    backend = asr_backend or os.getenv("ASR_BACKEND") or "mlx"
    if backend not in {"mlx", "paraformer"}:
        raise ValueError(f"Unknown ASR_BACKEND: {backend}")
    from .paraformer import ParaformerBackend

    default = (
        MlxWhisperBackend.default_model if backend == "mlx" else ParaformerBackend.default_model
    )
    model = asr_model or os.getenv("ASR_MODEL") or default
    if backend == "paraformer":
        cloud_asr_parameters(model)
    if backend == "mlx" and not ("whisper" in model.lower() or os.path.isdir(model)):
        raise ValueError("mlx backend requires a Whisper repository or local model directory")
    ocr = ocr_backend or os.getenv("OCR_BACKEND") or "rapidocr"
    if ocr != "rapidocr":
        raise ValueError(f"Unknown OCR_BACKEND: {ocr}")
    base = (os.getenv("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    url = urlsplit(base)
    if backend == "paraformer":
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("DASHSCOPE_BASE_URL must be an HTTPS base URL without credentials")
        if require_credentials and not os.getenv("DASHSCOPE_API_KEY", "").strip():
            raise ValueError("DASHSCOPE_API_KEY is required for paraformer")
    return {
        "asr_backend": backend,
        "asr_provider": "local" if backend == "mlx" else "dashscope",
        "asr_model": model,
        "asr_language": "zh",
        "asr_parameters": {} if backend == "mlx" else cloud_asr_parameters(model),
        "asr_base_url": None if backend == "mlx" else base,
        "ocr_backend": ocr,
        "ocr_model": OCR_MODEL,
    }
