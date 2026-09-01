import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.server as server
from backend.models import Rect, RenderRequest
from pydantic import ValidationError


class ServerContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        cls.auth = {"Authorization": f"Bearer {server.SESSION_TOKEN}"}

    def test_health_requires_bearer_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)

    def test_api_documentation_is_not_public(self):
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_health_lists_vietnamese_presets(self):
        response = self.client.get("/api/health", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([voice["id"] for voice in response.json()["voices"]], [
            "edge:vi-VN-HoaiMyNeural", "edge:vi-VN-NamMinhNeural"
        ])

    def test_cors_allows_extension_origin_only(self):
        allowed = self.client.options("/api/health", headers={
            "Origin": "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        denied = self.client.options("/api/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        self.assertNotEqual(denied.headers.get("access-control-allow-origin"), "https://evil.example")

    def test_rectangles_must_stay_inside_video(self):
        with self.assertRaises(ValidationError):
            Rect(x=.9, y=.1, w=.2, h=.2)

    def test_render_request_limits_blur_count(self):
        with self.assertRaises(ValidationError):
            RenderRequest(
                voiceMap={"*": "edge:vi-VN-HoaiMyNeural"},
                blurRegions=[{"x": 0, "y": 0, "w": .1, "h": .1}] * 21,
                subtitleRect={"x": .1, "y": .7, "w": .8, "h": .2},
            )

    def test_analysis_rejects_non_canonical_url(self):
        response = self.client.post("/api/jobs/analyze", headers=self.auth, json={
            "canonicalUrl": "https://evil.example/video/7674912144722875109",
            "cookieText": "cookie",
            "geminiApiKey": "not-a-real-key",
            "blurMode": "auto",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "INVALID_URL")

    def test_queued_job_does_not_claim_it_is_waiting_for_gpu(self):
        with patch.object(server.EXECUTOR, "submit"):
            response = self.client.post("/api/jobs/analyze", headers=self.auth, json={
                "canonicalUrl": "https://www.douyin.com/video/7674912144722875109",
                "cookieText": "cookie",
                "geminiApiKey": "not-a-real-key",
                "blurMode": "auto",
            })
        job_id = response.json()["jobId"]
        try:
            job = self.client.get(f"/api/jobs/{job_id}", headers=self.auth).json()
            self.assertEqual(job["status"], "queued")
            self.assertNotIn("GPU", job["message"])
            self.assertIn("phân tích", job["message"])
        finally:
            server.cleanup_job(job_id)

    def test_public_job_never_returns_credentials(self):
        public = server.public_job({
            "id": "job", "gemini_key": "secret-key", "cookie_text": "secret-cookie",
            "download_token": "secret-token", "preview_token": "preview-secret", "message": "ok",
        })
        self.assertEqual(public, {"id": "job", "message": "ok"})

    def test_preview_supports_http_byte_ranges_for_seeking(self):
        job_id = "preview-range-test"
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"0123456789")
            server.JOBS[job_id] = {
                "id": job_id, "status": "analysis_ready", "source": source,
                "canonical_url": "https://www.douyin.com/video/7671977232314797347",
            }
            try:
                created = self.client.post(f"/api/jobs/{job_id}/preview-token", headers=self.auth)
                self.assertEqual(created.status_code, 200)
                url = created.json()["url"].removeprefix(server.PUBLIC_URL)
                preview = self.client.get(url, headers={"Range": "bytes=2-5"})
                self.assertEqual(preview.status_code, 206)
                self.assertEqual(preview.content, b"2345")
                self.assertEqual(preview.headers.get("accept-ranges"), "bytes")
            finally:
                server.JOBS.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
