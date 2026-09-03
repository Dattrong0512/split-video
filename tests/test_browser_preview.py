import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.pipeline import create_browser_preview


class BrowserPreviewTest(unittest.TestCase):
    def test_browser_preview_is_h264_aac_and_faststart(self):
        with TemporaryDirectory() as directory:
            work_dir = Path(directory)
            source = work_dir / "source.mp4"
            source.write_bytes(b"source")
            commands = []

            def fake_run(command, code="PROCESSING_FAILED"):
                commands.append((command, code))
                Path(command[-1]).write_bytes(b"browser-compatible-preview")

            with patch("backend.pipeline.ffmpeg", return_value="ffmpeg"), patch(
                "backend.pipeline.run", side_effect=fake_run
            ):
                preview = create_browser_preview(source, work_dir)

            command, error_code = commands[0]
            self.assertEqual(preview, work_dir / "browser-preview.mp4")
            self.assertEqual(preview.read_bytes(), b"browser-compatible-preview")
            self.assertEqual(error_code, "PREVIEW_ENCODING_FAILED")
            self.assertIn("libx264", command)
            self.assertIn("aac", command)
            self.assertIn("yuv420p", command)
            self.assertIn("+faststart", command)


if __name__ == "__main__":
    unittest.main()
