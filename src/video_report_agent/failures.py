"""Stable failure codes and safe, actionable public descriptions."""


def asr_failure(error):
    code, http = error.provider_code, error.http_status
    if code == "SUCCESS_WITH_NO_VALID_FRAGMENT":
        return (
            "ASR_NO_VALID_SPEECH",
            "未检测到有效人声，无法生成报告。请更换包含清晰人声或可用字幕的视频。",
        )
    if code in {"Arrearage", "isv.OUT_OF_SERVICE"}:
        return (
            "ASR_BILLING_UNAVAILABLE",
            "系统语音识别服务因账户欠费或余额不足暂停，需管理员处理后再试。",
        )
    if http == 429 or (code or "").startswith("Throttling"):
        return "ASR_RATE_LIMITED", "语音识别服务请求过多，请稍后再试。"
    if http in {401, 403}:
        return "ASR_ACCESS_DENIED", "系统语音识别服务的认证或权限异常，需管理员检查。"
    if error.stage == "submit_unknown":
        return "ASR_SUBMIT_UNKNOWN", "语音识别提交结果暂时无法确认，请勿连续重试，需管理员核查。"
    return "ASR_SERVICE_ERROR", "语音识别服务未能完成处理，具体原因需管理员检查。"


def failure_group(status):
    if status.get("state") != "FAILED":
        return None
    category = status.get("error_category")
    if category == "INPUT_REJECTED" or status.get("error_code") == "VIDEO_DURATION_INVALID":
        return "input"
    if category == "EXTERNAL_API_FAILURE":
        return "external"
    if category in {"ENVIRONMENT_FAILURE", "SERVER_INTERRUPTED"}:
        return "runtime"
    if category in {
        "IMPLEMENTATION_FAILURE",
        "EXECUTION_FAILURE",
        "REPORT_INVALID",
        "EXTERNAL_MODEL_FAILURE",
    }:
        return "processing"
    return "unknown"
