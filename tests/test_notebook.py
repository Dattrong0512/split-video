import json
import shutil
import subprocess
import tempfile
import types
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
        cls.install_cell = "".join(next(
            cell.get("source", [])
            for cell in notebook["cells"]
            if cell.get("cell_type") == "code" and "ROOT = pathlib.Path" in "".join(cell.get("source", []))
        ))
        cls.checkout_source = cls.install_cell[
            cls.install_cell.index("ROOT = pathlib.Path"):
            cls.install_cell.index("requirements = ROOT")
        ]

    def test_each_run_starts_a_fresh_api_process(self):
        self.assertIn("if 'api_process' in globals(): stop_process(api_process)", self.launcher)
        self.assertIn("subprocess.Popen([sys.executable, '-m', 'uvicorn'", self.launcher)
        self.assertNotIn("import backend.server as server_module", self.launcher)
        self.assertNotIn("api_thread = threading.Thread", self.launcher)

    def test_fresh_api_uses_dynamic_port_and_current_tunnel_url(self):
        self.assertIn("port_socket.bind(('127.0.0.1', 0))", self.launcher)
        self.assertIn("'DUBBING_PUBLIC_URL': public_url", self.launcher)
        self.assertIn("f'http://127.0.0.1:{api_port}/api/health'", self.launcher)

    def test_checkout_retries_after_a_partial_clone_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout_root = Path(temp_dir) / "split-video"
            clone_attempts = 0

            def fake_run(command, **kwargs):
                nonlocal clone_attempts
                if command[:2] == ["git", "clone"]:
                    clone_attempts += 1
                    if clone_attempts == 1:
                        checkout_root.mkdir()
                        raise subprocess.CalledProcessError(
                            128,
                            command,
                            stderr="fatal: early EOF",
                        )
                return subprocess.CompletedProcess(command, 0)

            namespace = {
                "pathlib": types.SimpleNamespace(Path=lambda _path: checkout_root),
                "shutil": shutil,
                "subprocess": types.SimpleNamespace(
                    CalledProcessError=subprocess.CalledProcessError,
                    run=fake_run,
                ),
                "time": types.SimpleNamespace(sleep=lambda _seconds: None),
            }

            exec(self.checkout_source, namespace)
            self.assertEqual(clone_attempts, 2)

    def test_checkout_replaces_a_stale_non_repository_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout_root = Path(temp_dir) / "split-video"
            checkout_root.mkdir()
            stale_file = checkout_root / "incomplete-download"
            stale_file.write_text("partial", encoding="utf-8")
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0)

            namespace = {
                "pathlib": types.SimpleNamespace(Path=lambda _path: checkout_root),
                "shutil": shutil,
                "subprocess": types.SimpleNamespace(
                    CalledProcessError=subprocess.CalledProcessError,
                    run=fake_run,
                ),
                "time": types.SimpleNamespace(sleep=lambda _seconds: None),
            }

            exec(self.checkout_source, namespace)
            self.assertFalse(stale_file.exists())
            self.assertEqual([command[:2] for command in commands], [["git", "clone"]])

    def test_checkout_reports_git_stderr_after_retries_are_exhausted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout_root = Path(temp_dir) / "split-video"
            clone_attempts = 0

            def fake_run(command, **kwargs):
                nonlocal clone_attempts
                clone_attempts += 1
                checkout_root.mkdir()
                raise subprocess.CalledProcessError(
                    128,
                    command,
                    stderr="fatal: repository unavailable",
                )

            namespace = {
                "pathlib": types.SimpleNamespace(Path=lambda _path: checkout_root),
                "shutil": shutil,
                "subprocess": types.SimpleNamespace(
                    CalledProcessError=subprocess.CalledProcessError,
                    run=fake_run,
                ),
                "time": types.SimpleNamespace(sleep=lambda _seconds: None),
            }

            with self.assertRaisesRegex(
                RuntimeError,
                "SOURCE_CHECKOUT_FAILED: fatal: repository unavailable",
            ):
                exec(self.checkout_source, namespace)
            self.assertEqual(clone_attempts, 3)


if __name__ == "__main__":
    unittest.main()
