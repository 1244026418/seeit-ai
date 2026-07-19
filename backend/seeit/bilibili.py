from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yt_dlp


BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4v"}
BILIBILI_API_BASE = "https://api.bilibili.com"
BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}


class BilibiliImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedVideo:
    bvid: str
    title: str
    uploader: str
    duration_seconds: int
    cover_url: str
    path: Path


def normalize_bvid(value: str) -> str:
    match = BVID_PATTERN.search(value.strip())
    if not match:
        raise ValueError("请输入正确的 BV 号")
    bvid = match.group(0)
    return "BV" + bvid[2:]


def bilibili_video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{normalize_bvid(bvid)}"


def _options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "http_headers": BILIBILI_HEADERS,
    }


def _metadata(info: dict[str, Any], bvid: str) -> dict[str, Any]:
    cover_url = str(info.get("thumbnail") or "")[:1024]
    if cover_url.startswith("http://"):
        cover_url = "https://" + cover_url.removeprefix("http://")
    return {
        "bvid": bvid,
        "title": str(info.get("title") or bvid)[:255],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:100],
        "durationSeconds": max(0, int(info.get("duration") or 0)),
        "coverUrl": cover_url,
        "webpageUrl": bilibili_video_url(bvid),
    }


def _api_payload(path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{BILIBILI_API_BASE}{path}",
            params=params,
            headers=BILIBILI_HEADERS,
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise BilibiliImportError("B 站公开接口暂时不可用") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = str(payload.get("message") or "未知错误") if isinstance(payload, dict) else "响应格式异常"
        raise BilibiliImportError(f"B 站公开接口返回异常：{message[:120]}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BilibiliImportError("B 站公开接口未返回视频信息")
    return data


def _view_info(bvid: str) -> dict[str, Any]:
    return _api_payload("/x/web-interface/view", {"bvid": bvid})


def _metadata_from_view(info: dict[str, Any], bvid: str) -> dict[str, Any]:
    owner = info.get("owner") if isinstance(info.get("owner"), dict) else {}
    cover_url = str(info.get("pic") or "")[:1024]
    if cover_url.startswith("http://"):
        cover_url = "https://" + cover_url.removeprefix("http://")
    return {
        "bvid": bvid,
        "title": str(info.get("title") or bvid)[:255],
        "uploader": str(owner.get("name") or "")[:100],
        "durationSeconds": max(0, int(info.get("duration") or 0)),
        "coverUrl": cover_url,
        "webpageUrl": bilibili_video_url(bvid),
    }


def fetch_bilibili_metadata(value: str) -> dict[str, Any]:
    bvid = normalize_bvid(value)
    try:
        return _metadata_from_view(_view_info(bvid), bvid)
    except BilibiliImportError as api_error:
        fallback_error = api_error
    try:
        with yt_dlp.YoutubeDL({**_options(), "skip_download": True}) as downloader:
            info = downloader.extract_info(bilibili_video_url(bvid), download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise BilibiliImportError(
            "无法读取该公开视频；B 站公开接口和网页解析均失败，请稍后重试"
        ) from exc
    if not isinstance(info, dict):
        raise BilibiliImportError("B 站返回了无法识别的视频信息") from fallback_error
    return _metadata(info, bvid)


def _download_stream(url: str, destination: Path) -> None:
    try:
        with httpx.stream(
            "GET",
            url,
            headers=BILIBILI_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(120, connect=20),
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
    except (httpx.HTTPError, OSError) as exc:
        raise BilibiliImportError("B 站视频流下载失败") from exc


def _stream_url(stream: dict[str, Any]) -> str:
    return str(stream.get("baseUrl") or stream.get("base_url") or "")


def _download_via_public_api(bvid: str, directory: Path) -> DownloadedVideo:
    view = _view_info(bvid)
    pages = view.get("pages") if isinstance(view.get("pages"), list) else []
    first_page = pages[0] if pages and isinstance(pages[0], dict) else {}
    cid = int(first_page.get("cid") or view.get("cid") or 0)
    aid = int(view.get("aid") or 0)
    if not cid or not aid:
        raise BilibiliImportError("B 站公开接口未返回视频分集信息")

    play = _api_payload(
        "/x/player/playurl",
        {"avid": aid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 0, "qn": 80},
    )
    dash = play.get("dash") if isinstance(play.get("dash"), dict) else {}
    videos = dash.get("video") if isinstance(dash.get("video"), list) else []
    audios = dash.get("audio") if isinstance(dash.get("audio"), list) else []
    avc_videos = [
        item
        for item in videos
        if isinstance(item, dict)
        and int(item.get("id") or 0) <= 80
        and str(item.get("codecs") or "").lower().startswith("avc")
        and _stream_url(item)
    ]
    video_candidates = avc_videos or [
        item for item in videos if isinstance(item, dict) and int(item.get("id") or 0) <= 80 and _stream_url(item)
    ]
    audio_candidates = [item for item in audios if isinstance(item, dict) and _stream_url(item)]
    if not video_candidates or not audio_candidates:
        raise BilibiliImportError("B 站公开接口未返回可用的 DASH 音视频流")

    video = max(video_candidates, key=lambda item: (int(item.get("id") or 0), int(item.get("bandwidth") or 0)))
    audio = max(audio_candidates, key=lambda item: int(item.get("bandwidth") or 0))
    video_path = directory / f"{bvid}.video.m4s"
    audio_path = directory / f"{bvid}.audio.m4s"
    output_path = directory / f"{bvid}.mp4"
    try:
        _download_stream(_stream_url(video), video_path)
        _download_stream(_stream_url(audio), audio_path)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(video_path),
                    "-i",
                    str(audio_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise BilibiliImportError("FFmpeg 合并 B 站音视频流失败") from exc
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise BilibiliImportError("FFmpeg 未生成可用的 B 站视频文件")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)

    metadata = _metadata_from_view(view, bvid)
    return DownloadedVideo(
        bvid=bvid,
        title=metadata["title"],
        uploader=metadata["uploader"],
        duration_seconds=metadata["durationSeconds"],
        cover_url=metadata["coverUrl"],
        path=output_path,
    )


def download_bilibili_video(value: str, directory: Path) -> DownloadedVideo:
    bvid = normalize_bvid(value)
    directory.mkdir(parents=True, exist_ok=True)
    output_stem = directory / bvid
    options = {
        **_options(),
        "format": "bv*[height<=1080][vcodec^=avc]+ba/b[height<=1080][vcodec^=avc]/bv*[height<=1080]+ba/b[height<=1080]",
        "merge_output_format": "mp4",
        "outtmpl": str(output_stem) + ".%(ext)s",
        "concurrent_fragment_downloads": 2,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(bilibili_video_url(bvid), download=True)
    except yt_dlp.utils.DownloadError:
        return _download_via_public_api(bvid, directory)

    candidates = [
        path
        for path in directory.glob(f"{bvid}.*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        return _download_via_public_api(bvid, directory)
    path = max(candidates, key=lambda item: item.stat().st_size)
    metadata = _metadata(info if isinstance(info, dict) else {}, bvid)
    return DownloadedVideo(
        bvid=bvid,
        title=metadata["title"],
        uploader=metadata["uploader"],
        duration_seconds=metadata["durationSeconds"],
        cover_url=metadata["coverUrl"],
        path=path,
    )
