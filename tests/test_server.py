import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.server as server
from backend.models import AnalyzeRequest, Rect, RenderRequest
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
        self.assertEqual(response.json()["apiVersion"], "1.5.8")
        self.assertTrue(response.json()["immutableReviews"])
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

    def test_analysis_defaults_to_one_voice_and_limits_character_voices_to_four(self):
        request = AnalyzeRequest(
            canonicalUrl="https://www.douyin.com/video/7674912144722875109",
            cookieText="cookie",
            geminiApiKey="not-a-real-key",
        )
        self.assertEqual(request.voiceCount, 1)
        with self.assertRaises(ValidationError):
            AnalyzeRequest(
                canonicalUrl="https://www.douyin.com/video/7674912144722875109",
                cookieText="cookie",
                geminiApiKey="not-a-real-key",
                voiceCount=5,
            )

    def test_render_request_limits_blur_count(self):
        with self.assertRaises(ValidationError):
            RenderRequest(
                voiceMap={"*": "edge:vi-VN-HoaiMyNeural"},
                blurRegions=[{"x": 0, "y": 0, "w": .1, "h": .1}] * 21,
                subtitleRect={"x": .1, "y": .7, "w": .8, "h": .2},
            )
        with self.assertRaises(ValidationError):
            RenderRequest(
                voiceMap={"*": "edge:vi-VN-HoaiMyNeural"},
                subtitleRect={"x": .1, "y": .7, "w": .8, "h": .2}, speechRate=1.5,
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
                "voiceCount": 3,
            })
        job_id = response.json()["jobId"]
        try:
            job = self.client.get(f"/api/jobs/{job_id}", headers=self.auth).json()
            self.assertEqual(job["status"], "queued")
            self.assertNotIn("GPU", job["message"])
            self.assertIn("phân tích", job["message"])
            self.assertEqual(job["voice_count"], 3)
        finally:
            server.cleanup_job(job_id)

    def test_public_job_never_returns_credentials(self):
        public = server.public_job({
            "id": "job", "gemini_key": "secret-key", "cookie_text": "secret-cookie",
            "download_token": "secret-token", "preview_token": "preview-secret",
            "browser_preview": Path("browser-preview.mp4"), "message": "ok",
            "media_url": "https://example/signature-secret", "browser_user_agent": "browser",
        })
        self.assertEqual(public, {"id": "job", "message": "ok"})

    def test_preview_supports_http_byte_ranges_for_seeking(self):
        job_id = "preview-range-test"
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"unsupported-source-codec")
            browser_preview = Path(directory) / "browser-preview.mp4"
            browser_preview.write_bytes(b"0123456789")
            server.JOBS[job_id] = {
                "id": job_id, "status": "analysis_ready", "source": source,
                "browser_preview": browser_preview,
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

    def test_dub_review_token_serves_30_second_preview(self):
        job_id = "dub-review-test"
        with TemporaryDirectory() as directory:
            result = Path(directory) / "review.mp4"
            result.write_bytes(b"review-video")
            server.JOBS[job_id] = {
                "id": job_id, "status": "preview_ready", "review_result": result,
                "review_rate": 1.15, "duration": 70,
            }
            try:
                created = self.client.post(f"/api/jobs/{job_id}/review-token", headers=self.auth)
                self.assertEqual(created.status_code, 200)
                self.assertEqual(created.json()["seconds"], 30)
                self.assertEqual(created.json()["speechRate"], 1.15)
                url = created.json()["url"].removeprefix(server.PUBLIC_URL)
                review = self.client.get(url)
                self.assertEqual(review.status_code, 200)
                self.assertEqual(review.content, b"review-video")
            finally:
                server.JOBS.pop(job_id, None)

    def test_preview_render_can_be_repeated_before_full_render(self):
        job_id = "preview-render-state-test"
        with TemporaryDirectory() as directory:
            server.JOBS[job_id] = {
                "id": job_id, "status": "preview_ready", "work_dir": Path(directory),
            }
            body = {
                "voiceMap": {"*": "edge:vi-VN-HoaiMyNeural"},
                "blurRegions": [], "subtitleRect": {"x": .1, "y": .7, "w": .8, "h": .2},
                "speechRate": 1.2, "previewOnly": True,
            }
            try:
                with patch.object(server.EXECUTOR, "submit") as submit:
                    response = self.client.post(f"/api/jobs/{job_id}/render", headers=self.auth, json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(server.JOBS[job_id]["status"], "queued_preview")
                self.assertTrue(submit.call_args.args[2].previewOnly)
            finally:
                server.JOBS.pop(job_id, None)

    def test_review_links_keep_their_audio_version_across_rerenders_and_tabs(self):
        job_id = "immutable-review-test"
        with TemporaryDirectory() as directory:
            old = Path(directory) / "old.mp4"
            new = Path(directory) / "new.mp4"
            old.write_bytes(b"old-audio")
            new.write_bytes(b"new-audio")
            server.JOBS[job_id] = {"id": job_id, "status": "preview_ready", "review_result": old, "duration": 40}
            try:
                first = self.client.post(f"/api/jobs/{job_id}/review-token", headers=self.auth).json()["url"]
                server.JOBS[job_id]["review_result"] = new
                second = self.client.post(f"/api/jobs/{job_id}/review-token", headers=self.auth).json()["url"]
                self.assertEqual(self.client.get(first).content, b"old-audio")
                self.assertEqual(self.client.get(second).content, b"new-audio")
                self.assertNotIn("review_links", server.public_job(server.JOBS[job_id]))
                self.assertEqual(self.client.get(f"/api/reviews/{job_id}?token=wrong").status_code, 403)
            finally:
                server.JOBS.pop(job_id, None)

    def test_timing_failure_preserves_analysis_and_allows_retry(self):
        job_id = "timing-retry-test"
        request = RenderRequest(voiceMap={"*": "edge:vi-VN-HoaiMyNeural"},
                                subtitleRect={"x": .1, "y": .7, "w": .8, "h": .2}, previewOnly=True)
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"source")
            server.JOBS[job_id] = {"id": job_id, "status": "rendering_preview", "work_dir": Path(directory)}
            try:
                with patch.object(server, "render_job", side_effect=server.PipelineError("TTS_TIMING_OVERFLOW", "Câu quá dài")):
                    server.run_render(job_id, request)
                self.assertEqual(server.JOBS[job_id]["status"], "render_retry")
                self.assertTrue(source.exists())
                with patch.object(server.EXECUTOR, "submit"):
                    response = self.client.post(f"/api/jobs/{job_id}/render", headers=self.auth, json=request.model_dump())
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("error", server.JOBS[job_id])
            finally:
                server.JOBS.pop(job_id, None)

    def test_render_rejects_more_or_fewer_voices_than_the_analysis_configuration(self):
        job_id = "voice-count-contract-test"
        server.JOBS[job_id] = {"id": job_id, "status": "analysis_ready", "voice_count": 1}
        body = {
            "voiceMap": {"S1": "edge:vi-VN-HoaiMyNeural", "S2": "edge:vi-VN-NamMinhNeural"},
            "blurRegions": [], "subtitleRect": {"x": .1, "y": .7, "w": .8, "h": .2},
            "previewOnly": True,
        }
        try:
            with patch.object(server.EXECUTOR, "submit"):
                response = self.client.post(f"/api/jobs/{job_id}/render", headers=self.auth, json=body)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"]["code"], "INVALID_VOICE_MAP")
        finally:
            server.JOBS.pop(job_id, None)

    def test_render_rejects_duplicate_voices_when_two_are_configured(self):
        job_id = "distinct-voice-contract-test"
        server.JOBS[job_id] = {"id": job_id, "status": "analysis_ready", "voice_count": 2}
        body = {
            "voiceMap": {"S1": "edge:vi-VN-HoaiMyNeural", "S2": "edge:vi-VN-HoaiMyNeural"},
            "blurRegions": [], "subtitleRect": {"x": .1, "y": .7, "w": .8, "h": .2},
            "previewOnly": True,
        }
        try:
            with patch.object(server.EXECUTOR, "submit"):
                response = self.client.post(f"/api/jobs/{job_id}/render", headers=self.auth, json=body)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"]["code"], "INVALID_VOICE_MAP")
        finally:
            server.JOBS.pop(job_id, None)

    def test_render_accepts_exactly_two_distinct_voices_when_two_are_configured(self):
        job_id = "valid-distinct-voice-contract-test"
        server.JOBS[job_id] = {"id": job_id, "status": "analysis_ready", "voice_count": 2}
        body = {
            "voiceMap": {"S1": "edge:vi-VN-HoaiMyNeural", "S2": "edge:vi-VN-NamMinhNeural"},
            "blurRegions": [], "subtitleRect": {"x": .1, "y": .7, "w": .8, "h": .2},
            "previewOnly": True,
        }
        try:
            with patch.object(server.EXECUTOR, "submit") as submit:
                response = self.client.post(f"/api/jobs/{job_id}/render", headers=self.auth, json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(submit.call_args.args[2].voiceMap, body["voiceMap"])
        finally:
            server.JOBS.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
