import json
import unittest
from pathlib import Path


class NotebookLauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        notebook = json.loads(Path("OmniVoice_API.ipynb").read_text(encoding="utf-8"))
        cls.launcher = "\n".join(
            "".join(cell.get("source", []))
            for cell in notebook["cells"] if cell.get("cell_type") == "code"
        )

    def test_each_run_starts_a_fresh_api_process(self):
        self.assertIn("if 'api_process' in globals(): stop_process(api_process)", self.launcher)
        self.assertIn("subprocess.Popen([sys.executable, '-m', 'uvicorn'", self.launcher)
        self.assertNotIn("import backend.server as server_module", self.launcher)
        self.assertNotIn("api_thread = threading.Thread", self.launcher)

    def test_fresh_api_uses_dynamic_port_and_current_tunnel_url(self):
        self.assertIn("port_socket.bind(('127.0.0.1', 0))", self.launcher)
        self.assertIn("'DUBBING_PUBLIC_URL': public_url", self.launcher)
        self.assertIn("f'http://127.0.0.1:{api_port}/api/health'", self.launcher)


if __name__ == "__main__":
    unittest.main()
