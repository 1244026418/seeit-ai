from __future__ import annotations

from pathlib import Path

import pytest

from seeit import bilibili


def _view_payload() -> dict:
    return {
        "aid": 123456,
        "title": "公开测试视频",
        "duration": 172,
        "pic": "http://i0.hdslb.com/test.jpg",
        "owner": {"name": "测试作者"},
        "pages": [{"cid": 987654, "page": 1}],
    }


def test_metadata_prefers_public_api_over_webpage_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bilibili, "_view_info", lambda bvid: _view_payload())

    class UnexpectedYoutubeDL:
        def __init__(self, *args, **kwargs):
            raise AssertionError("官方 API 成功时不应调用网页提取器")

    monkeypatch.setattr(bilibili.yt_dlp, "YoutubeDL", UnexpectedYoutubeDL)

    metadata = bilibili.fetch_bilibili_metadata("BV1wMduBNEPS")

    assert metadata == {
        "bvid": "BV1wMduBNEPS",
        "title": "公开测试视频",
        "uploader": "测试作者",
        "durationSeconds": 172,
        "coverUrl": "https://i0.hdslb.com/test.jpg",
        "webpageUrl": "https://www.bilibili.com/video/BV1wMduBNEPS",
    }


def test_metadata_falls_back_to_ytdlp_when_public_api_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bilibili,
        "_view_info",
        lambda bvid: (_ for _ in ()).throw(bilibili.BilibiliImportError("api down")),
    )

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            assert url.endswith("BV1wMduBNEPS")
            assert download is False
            return {
                "title": "网页回退视频",
                "uploader": "网页作者",
                "duration": 60,
                "thumbnail": "http://i0.hdslb.com/fallback.jpg",
            }

    monkeypatch.setattr(bilibili.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    metadata = bilibili.fetch_bilibili_metadata("BV1wMduBNEPS")

    assert metadata["title"] == "网页回退视频"
    assert metadata["durationSeconds"] == 60
    assert metadata["coverUrl"] == "https://i0.hdslb.com/fallback.jpg"


def test_download_falls_back_to_public_dash_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        bilibili,
        "_view_info",
        lambda bvid: _view_payload(),
    )

    def fake_api_payload(path: str, params: dict) -> dict:
        assert path == "/x/player/playurl"
        assert params["avid"] == 123456
        assert params["cid"] == 987654
        return {
            "dash": {
                "video": [
                    {"id": 64, "bandwidth": 600000, "codecs": "avc1.64001f", "baseUrl": "https://video"},
                    {"id": 80, "bandwidth": 1000000, "codecs": "hev1.1.6.L120.90", "baseUrl": "https://hevc"},
                ],
                "audio": [{"id": 30280, "bandwidth": 128000, "baseUrl": "https://audio"}],
            },
        }

    monkeypatch.setattr(bilibili, "_api_payload", fake_api_payload)

    class FailingYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            raise bilibili.yt_dlp.utils.DownloadError("webpage blocked")

    monkeypatch.setattr(bilibili.yt_dlp, "YoutubeDL", FailingYoutubeDL)

    streams: list[tuple[str, str]] = []

    def fake_download_stream(url: str, destination: Path) -> None:
        streams.append((url, destination.name))
        destination.write_bytes(b"stream")

    monkeypatch.setattr(bilibili, "_download_stream", fake_download_stream)

    def fake_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"merged mp4")

    monkeypatch.setattr(bilibili.subprocess, "run", fake_ffmpeg)

    downloaded = bilibili.download_bilibili_video("BV1wMduBNEPS", tmp_path)

    assert streams == [
        ("https://video", "BV1wMduBNEPS.video.m4s"),
        ("https://audio", "BV1wMduBNEPS.audio.m4s"),
    ]
    assert downloaded.path.name == "BV1wMduBNEPS.mp4"
    assert downloaded.path.read_bytes() == b"merged mp4"
