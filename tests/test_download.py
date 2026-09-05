import io
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import MagicMock, patch
from urllib.request import Request

from backend.pipeline import (
    DouyinMediaRedirectHandler, PipelineError, allowed_douyin_media_url,
    download_browser_media, download_douyin,
    media_duration,
)


class DouyinDownloadTest(unittest.TestCase):
    media_url = "https://v3.douyinvod.com/video.mp4?signature=private"

    def job(self, directory):
        return {"work_dir": Path(directory), "canonical_url": "https://www.douyin.com/video/7649625894688269809",
                "cookie_text": "# Netscape HTTP Cookie File\n", "media_url": self.media_url,
                "browser_user_agent": "Chrome-Test"}

    def test_browser_media_succeeds_without_douyin_metadata_api(self):
        with TemporaryDirectory() as directory:
            job = self.job(directory)
            with patch("backend.pipeline.download_browser_media", return_value=Path(directory) / "source.mp4") as direct:
                self.assertEqual(download_douyin(job), Path(directory) / "source.mp4")
            direct.assert_called_once_with(job, self.media_url, "Chrome-Test")
            self.assertFalse((Path(directory) / "cookies.txt").exists())
            self.assertNotIn("cookie_text", job)
            self.assertNotIn("media_url", job)

    def test_expired_media_falls_back_to_extractor_with_browser_headers_and_cookies(self):
        with TemporaryDirectory() as directory:
            job = self.job(directory)
            source = Path(directory) / "source.mp4"
            module = ModuleType("yt_dlp")
            downloader = MagicMock()
            module.YoutubeDL = MagicMock(return_value=downloader)
            downloader.__enter__.return_value = downloader

            def extract(url, download):
                self.assertEqual(url, job["canonical_url"])
                self.assertTrue(download)
                self.assertTrue((Path(directory) / "cookies.txt").exists())
                source.write_bytes(b"source")
                return {"id": "video"}

            downloader.extract_info.side_effect = extract
            downloader.prepare_filename.return_value = str(source)
            with patch.dict("sys.modules", {"yt_dlp": module}), \
                    patch("backend.pipeline.download_browser_media", side_effect=RuntimeError("403")):
                self.assertEqual(download_douyin(job), source)
            headers = module.YoutubeDL.call_args.args[0]["http_headers"]
            self.assertEqual(headers["User-Agent"], "Chrome-Test")
            self.assertFalse((Path(directory) / "cookies.txt").exists())

    def test_metadata_rejection_is_not_reported_as_expired_login(self):
        for detail in ["Fresh cookies (not necessarily logged in) are needed", "HTTP Error 403", "Failed to download web detail JSON"]:
            with self.subTest(detail=detail), TemporaryDirectory() as directory:
                job = self.job(directory)
                job["media_url"] = None
                module = ModuleType("yt_dlp")
                module.YoutubeDL = MagicMock(side_effect=RuntimeError(detail))
                with patch.dict("sys.modules", {"yt_dlp": module}), self.assertRaises(PipelineError) as raised:
                    download_douyin(job)
                self.assertEqual(raised.exception.code, "DOUYIN_ACCESS_BLOCKED")
                self.assertFalse((Path(directory) / "cookies.txt").exists())

    def test_cancellation_does_not_fall_back_to_another_download(self):
        with TemporaryDirectory() as directory:
            with patch("backend.pipeline.download_browser_media", side_effect=PipelineError("CANCELLED", "Đã hủy")), \
                    self.assertRaises(PipelineError) as raised:
                download_douyin(self.job(directory))
            self.assertEqual(raised.exception.code, "CANCELLED")

    def test_direct_urls_and_redirects_cannot_target_arbitrary_hosts(self):
        self.assertTrue(allowed_douyin_media_url(self.media_url))
        for url in ["http://v3.douyinvod.com/video.mp4", "https://localhost/video", "https://127.0.0.1/",
                    "https://douyinvod.com.evil.example/video", "file:///etc/passwd", "https://user:pass@v3.douyinvod.com/video",
                    "https://v3.douyinvod.com:8080/video", "https://v3.douyinvod.com/\r\nHeader:value"]:
            with self.subTest(url=url):
                self.assertFalse(allowed_douyin_media_url(url))
                with self.assertRaises(PipelineError):
                    DouyinMediaRedirectHandler().redirect_request(Request(self.media_url), None, 302, "Found", {}, url)

    def test_streaming_download_remuxes_both_tracks_and_never_forwards_login_cookies(self):
        with TemporaryDirectory() as directory:
            response = io.BytesIO(b"media-bytes")
            response.status = 200
            response.headers = {"Content-Length": "11", "Content-Type": "video/mp4"}
            response.geturl = lambda: self.media_url
            opener = MagicMock()
            opener.open.return_value = response

            def remux(command, code):
                self.assertEqual((Path(directory) / "browser-media.part").read_bytes(), b"media-bytes")
                self.assertIn("0:v:0", command)
                self.assertIn("0:a:0", command)
                self.assertIn("file,pipe", command)
                Path(command[-1]).write_bytes(b"mp4")

            with patch("backend.pipeline.build_opener", return_value=opener), \
                    patch("backend.pipeline.ffmpeg", return_value="ffmpeg"), patch("backend.pipeline.run", side_effect=remux):
                result = download_browser_media(self.job(directory), self.media_url, "Chrome-Test")
            self.assertEqual(result.read_bytes(), b"mp4")
            self.assertIsNone(opener.open.call_args.args[0].get_header("Cookie"))
            self.assertFalse((Path(directory) / "browser-media.part").exists())

    def test_truncated_or_html_responses_never_reach_media_processing(self):
        for headers in [{"Content-Length": "100", "Content-Type": "video/mp4"}, {"Content-Type": "text/html"}]:
            with self.subTest(headers=headers), TemporaryDirectory() as directory:
                response = io.BytesIO(b"partial")
                response.status = 200
                response.headers = headers
                response.geturl = lambda: self.media_url
                opener = MagicMock()
                opener.open.return_value = response
                with patch("backend.pipeline.build_opener", return_value=opener), patch("backend.pipeline.run") as run, \
                        self.assertRaises(PipelineError):
                    download_browser_media(self.job(directory), self.media_url, "Chrome-Test")
                run.assert_not_called()
                self.assertFalse((Path(directory) / "browser-media.part").exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_real_mp4_from_browser_transport_preserves_audio_and_video(self):
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(fixture),
            ], check=True)
            data = fixture.read_bytes()
            response = io.BytesIO(data)
            response.status = 200
            response.headers = {"Content-Length": str(len(data)), "Content-Type": "video/mp4"}
            response.geturl = lambda: self.media_url
            opener = MagicMock()
            opener.open.return_value = response
            with patch("backend.pipeline.build_opener", return_value=opener):
                result = download_browser_media(self.job(directory), self.media_url, "Chrome-Test")
            self.assertAlmostEqual(media_duration(result), 1, delta=.1)


if __name__ == "__main__":
    unittest.main()
