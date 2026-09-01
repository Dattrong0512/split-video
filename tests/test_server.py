import unittest

from fastapi.testclient import TestClient

import backend.server as server


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


if __name__ == "__main__":
    unittest.main()
