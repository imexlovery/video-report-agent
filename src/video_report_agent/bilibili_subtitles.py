"""Read Bilibili subtitle tracks and AI captions without downloading media."""

import hashlib
import math
import os
import time
from urllib.parse import urlencode, urlsplit

import httpx

from .ingest import MAX_VIDEO_SECONDS, UrlIngestError

API = "https://api.bilibili.com"
SUBTITLE_SOURCES = ("subtitle_track", "bilibili_ai")
SOURCE_NAMES = {
    "asr": "ASR 语音识别",
    "subtitle_track": "字幕轨",
    "bilibili_ai": "B站 AI 字幕",
    "ocr": "画面 OCR",
}
WBI_ORDER = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
)


class SubtitleError(UrlIngestError):
    def __init__(self, code, message):
        super().__init__(code, message)


def validate_sources(sources):
    if (
        not isinstance(sources, list)
        or not sources
        or any(
            not isinstance(item, str) or item not in SOURCE_NAMES or item == "ocr"
            for item in sources
        )
        or len(set(sources)) != len(sources)
    ):
        raise ValueError(
            "transcript_sources 请选择不重复的 asr、subtitle_track 或 bilibili_ai；OCR 暂不启用"
        )
    return sources


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("invalid protobuf varint")


def _fields(data):
    """Decode the wire types used by subtitle metadata, retaining repeated fields."""
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, offset = _varint(data, offset)
            else:
                length = 8 if wire == 1 else 4
            if offset + length > len(data):
                raise ValueError("truncated protobuf field")
            value, offset = data[offset : offset + length], offset + length
        else:
            raise ValueError("unsupported protobuf wire type")
        yield number, wire, value


def parse_web_tracks(data):
    # Field layout verified against ccBilly-aipm/bilibili-ai-subtitle (MIT).
    # This small decoder uses the same wire contract; see docs/subtitle-sources.md.
    tracks = []
    for number, wire, message in _fields(data):
        if number != 1 or wire != 2:
            continue
        for number, wire, track in _fields(message):
            if number != 3 or wire != 2:
                continue
            fields = {number: value for number, _, value in _fields(track)}
            if 3 in fields and 5 in fields:
                tracks.append(
                    {
                        "id": fields.get(1),
                        "lan": fields[3].decode("utf-8"),
                        "lan_doc": fields.get(4, b"").decode("utf-8"),
                        "subtitle_url": fields[5].decode("utf-8"),
                    }
                )
    return tracks


def read_sessdata(browser=None):
    value = os.getenv("BILIBILI_SESSDATA", "").strip()
    if value:
        return value
    browser = browser or os.getenv("BILIBILI_SUBTITLE_BROWSER", "").strip()
    if not browser:
        return None
    from yt_dlp.cookies import extract_cookies_from_browser

    try:
        return next(
            (
                cookie.value
                for cookie in extract_cookies_from_browser(browser)
                if cookie.name == "SESSDATA" and cookie.domain.lstrip(".") == "bilibili.com"
            ),
            None,
        )
    except Exception:
        raise SubtitleError(
            "SUBTITLE_LOGIN_REQUIRED", "无法读取 B站登录态，请检查所选浏览器或配置。"
        ) from None


def validate_cues(body, duration_ms):
    if not isinstance(body, list) or not body:
        raise ValueError("empty subtitle")
    result = []
    previous = -1
    for index, cue in enumerate(body):
        start, end = float(cue["from"]), float(cue["to"])
        text = cue["content"]
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or start < previous
            or end * 1000 > duration_ms + 5000
            or not isinstance(text, str)
        ):
            raise ValueError("invalid subtitle cue")
        if text.strip():
            result.append({"from": start, "to": end, "content": text, "cue_index": index})
        previous = start
    if not result:
        raise ValueError("empty subtitle")
    return result


def coverage(body, duration_ms):
    first, last = round(body[0]["from"] * 1000), round(max(c["to"] for c in body) * 1000)
    end, covered, largest_gap = 0, 0, 0
    for cue in body:
        start, stop = round(cue["from"] * 1000), round(cue["to"] * 1000)
        largest_gap = max(largest_gap, start - end)
        covered += max(0, stop - max(start, end))
        end = max(end, stop)
    return {
        "first_ms": first,
        "last_ms": last,
        "tail_gap_ms": max(0, duration_ms - last),
        "largest_gap_ms": largest_gap,
        "timed_ratio": round(min(covered / duration_ms, 1), 4),
    }


class BilibiliSubtitles:
    def __init__(self, *, browser=None, client=None, authenticate=True):
        sessdata = read_sessdata(browser) if authenticate else None
        self.client = client or httpx.Client(
            timeout=15,
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
        )
        self.owns_client = client is None
        if sessdata:
            self.client.cookies.set("SESSDATA", sessdata, domain="api.bilibili.com")
        self.nav = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.owns_client:
            self.client.close()

    def _get(self, url, **kwargs):
        try:
            response = self.client.get(url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            # Signed URLs and authentication never enter logs or persisted errors.
            raise SubtitleError(
                "SUBTITLE_FETCH_FAILED", "B站字幕接口请求失败，请检查网络后重试。"
            ) from None

    def _json(self, path, **kwargs):
        try:
            payload = self._get(API + path, **kwargs).json()
            if payload.get("code") != 0:
                code = payload.get("code")
                raise SubtitleError(
                    "SUBTITLE_LOGIN_REQUIRED" if code in (-101, -111) else "SUBTITLE_FETCH_FAILED",
                    "B站接口需要登录。" if code in (-101, -111) else f"B站接口返回错误（{code}）。",
                )
            return payload["data"]
        except (ValueError, KeyError, TypeError):
            raise SubtitleError("SUBTITLE_FETCH_FAILED", "B站接口返回了无效数据。") from None

    def video_info(self, source):
        data = self._json("/x/web-interface/view", params={"bvid": source.bvid})
        pages = data.get("pages", [])
        if data.get("bvid") != source.bvid or not 1 <= source.page_number <= len(pages):
            raise SubtitleError("URL_INVALID", "视频信息或分 P 与请求不一致。")
        page = pages[source.page_number - 1]
        duration = page.get("duration")
        if not isinstance(duration, (int, float)) or not 0 < duration <= MAX_VIDEO_SECONDS:
            raise SubtitleError("VIDEO_DURATION_INVALID", "视频时长无效或超过 5 小时。")
        title = data["title"]
        if len(pages) > 1:
            title += f" - P{source.page_number} {page.get('part', '')}"
        uploader = data.get("owner", {}).get("name", "")
        return {
            "video_id": source.video_id,
            "bvid": source.bvid,
            "aid": data["aid"],
            "cid": page["cid"],
            "page": source.page_number,
            "duration_ms": round(duration * 1000),
            "title": title,
            "uploader": uploader,
            "attribution": f"Bilibili；{uploader}；《{title}》；{source.canonical_url}",
        }

    def tracks(self, info):
        if self.nav is None:
            # Anonymous nav returns code -101 but still supplies WBI keys.
            self.nav = self._get(API + "/x/web-interface/nav").json().get("data", {})
        logged_in = bool(self.nav.get("isLogin"))
        tracks, failures = [], []
        try:
            key_data = self.nav["wbi_img"]
            lookup = "".join(
                urlsplit(key_data[k]).path.rsplit("/", 1)[-1].split(".")[0]
                for k in ("img_url", "sub_url")
            )
            key = "".join(lookup[i] for i in WBI_ORDER)
            params = {
                "aid": info["aid"],
                "bvid": info["bvid"],
                "cid": info["cid"],
                "wts": round(time.time()),
            }
            query = urlencode(sorted(params.items()))
            params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
            data = self._json("/x/player/wbi/v2", params=params)
            if data.get("aid") != info["aid"] or data.get("cid") != info["cid"]:
                raise ValueError("subtitle identity mismatch")
            tracks = data.get("subtitle", {}).get("subtitles", [])
        except (SubtitleError, KeyError, ValueError, TypeError, IndexError):
            failures.append("PLAYER_SUBTITLE_FAILED")
        # Query the new interface even when the old one only has a manual track.
        try:
            response = self._get(
                API + "/x/v2/subtitle/web/view",
                params={
                    "oid": info["cid"],
                    "pid": info["aid"],
                    "context_ext": '{"video_type":1}',
                    "type": 1,
                    "cur_production_type": 0,
                    "preferred_language": "ai-zh",
                    "playlist_switch": 0,
                },
                headers={"Accept": "application/octet-stream"},
            )
            tracks += parse_web_tracks(response.content)
        except (SubtitleError, ValueError, TypeError):
            failures.append("WEB_SUBTITLE_FAILED")
        unique = {}
        for track in tracks:
            if track.get("subtitle_url"):
                unique.setdefault((str(track.get("id")), track.get("lan")), track)
        return list(unique.values()), logged_in, failures

    def fetch(self, source, selected=SUBTITLE_SOURCES):
        info = self.video_info(source)
        if not selected:
            return {**info, "sources": {}, "authenticated": None, "warnings": []}
        tracks, logged_in, failures = self.tracks(info)
        snapshot = {**info, "sources": {}, "authenticated": logged_in, "warnings": failures}
        for kind in selected:
            if kind not in SUBTITLE_SOURCES:
                continue
            candidates = [
                t
                for t in tracks
                if (
                    t.get("lan") == "ai-zh"
                    if kind == "bilibili_ai"
                    else not str(t.get("lan", "")).startswith("ai-")
                )
            ]
            candidates.sort(
                key=lambda t: (
                    0 if t.get("lan") in ("zh-CN", "zh-Hans", "zh", "ai-zh") else 1,
                    str(t.get("lan", "")),
                )
            )
            result = {"status": "OFF", "count": 0}
            if not candidates:
                result["status"] = (
                    "LOGIN_REQUIRED" if not logged_in else "FAILED" if failures else "ABSENT"
                )
            else:
                track = candidates[0]
                result.update(language=track.get("lan"), track_id=str(track.get("id")))
                try:
                    url = track["subtitle_url"]
                    url = "https:" + url if url.startswith("//") else url
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname
                        not in {
                            "aisubtitle.hdslb.com",
                            "i0.hdslb.com",
                            "i1.hdslb.com",
                            "i2.hdslb.com",
                            "subtitle.bilibili.com",
                        }
                        or parsed.username
                        or parsed.password
                        or parsed.port
                    ):
                        raise ValueError("unexpected subtitle URL")
                    raw_body = self._get(url).json().get("body")
                    body = validate_cues(raw_body, info["duration_ms"])
                    result.update(
                        status="READY",
                        count=len(body),
                        empty_cue_count=len(raw_body) - len(body),
                        body=body,
                        coverage=coverage(body, info["duration_ms"]),
                    )
                except SubtitleError as exc:
                    result.update(status="FAILED", error_code=exc.category)
                except (ValueError, KeyError, TypeError):
                    result.update(status="FAILED", error_code="SUBTITLE_DATA_INVALID")
            snapshot["sources"][kind] = result
        return snapshot


def fetch_subtitles(source, selected=SUBTITLE_SOURCES, *, browser=None):
    with BilibiliSubtitles(browser=browser, authenticate=bool(selected)) as client:
        return client.fetch(source, selected)
