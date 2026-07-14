from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp


BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4v"}


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


def fetch_bilibili_metadata(value: str) -> dict[str, Any]:
    bvid = normalize_bvid(value)
    try:
        with yt_dlp.YoutubeDL({**_options(), "skip_download": True}) as downloader:
            info = downloader.extract_info(bilibili_video_url(bvid), download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise BilibiliImportError("无法读取该公开视频，请检查 BV 号或稍后重试") from exc
    if not isinstance(info, dict):
        raise BilibiliImportError("B 站返回了无法识别的视频信息")
    return _metadata(info, bvid)


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
    except yt_dlp.utils.DownloadError as exc:
        raise BilibiliImportError("公开视频下载失败，请稍后重试") from exc

    candidates = [
        path
        for path in directory.glob(f"{bvid}.*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        raise BilibiliImportError("下载完成后未找到可处理的视频文件")
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
