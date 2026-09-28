import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


MODULE_PATH = Path(__file__).resolve().parents[1] / "routes.py"
SPEC = importlib.util.spec_from_file_location("visual_export_routes", MODULE_PATH)
routes = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(routes)


class RoutesTest(unittest.TestCase):
    def test_export_settings_schema(self):
        self.assertTrue(routes._is_valid_setting("width", 1920))
        self.assertTrue(routes._is_valid_setting("height", 1080))
        self.assertTrue(routes._is_valid_setting("fps", 60))
        self.assertTrue(routes._is_valid_setting("bitrate_mbps", 10))
        self.assertTrue(routes._is_valid_setting("include_chrome", False))
        self.assertFalse(routes._is_valid_setting("fps", 59))
        self.assertFalse(routes._is_valid_setting("bitrate_mbps", 1000))
        self.assertFalse(routes._is_valid_setting("unknown", True))

    def test_setup_registers_settings_capabilities_and_mux_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = FastAPI()
            routes.setup(app, {"config_dir": Path(directory)})
            registered = {
                (route.path, method)
                for route in app.routes
                for method in (route.methods or set())
            }
        base = "/api/plugins/visual_export"
        self.assertIn((f"{base}/settings", "GET"), registered)
        self.assertIn((f"{base}/settings", "POST"), registered)
        self.assertIn((f"{base}/capabilities", "GET"), registered)
        self.assertIn((f"{base}/mux", "POST"), registered)


def _client(tmp_path):
    app = FastAPI()
    routes.setup(app, {"config_dir": tmp_path})
    return TestClient(app)


def _export(client, *, fps="30", video=b"video", audio=b"audio", filename="song"):
    return client.post(
        "/api/plugins/visual_export/mux",
        data={"fps": fps, "duration": "1", "filename": filename},
        files={
            "video": ("frames.h264", video, "video/h264"),
            "audio": ("song.wav", audio, "audio/wav"),
        },
    )


def test_mux_rejects_invalid_fps_before_creating_work_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")

    def unexpected_work_dir(**kwargs):
        raise AssertionError("invalid timing must not create a work directory")

    monkeypatch.setattr(routes.tempfile, "mkdtemp", unexpected_work_dir)
    response = _export(_client(tmp_path), fps="25")
    assert response.status_code == 400
    assert response.json() == {"error": "invalid export timing"}


def test_mux_rejects_oversized_upload_and_removes_work_dir(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes, "MAX_VIDEO_BYTES", 3)
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))

    response = _export(_client(tmp_path), video=b"four")
    assert response.status_code == 400
    assert "too large" in response.json()["error"]
    assert not work.exists()


def test_mux_returns_mp4_and_removes_work_dir(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))

    def fake_ffmpeg(cmd, **kwargs):
        assert (work / "frames.h264").read_bytes() == b"video"
        assert (work / "audio.bin").read_bytes() == b"audio"
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_ffmpeg)
    response = _export(_client(tmp_path), filename="song / export")
    assert response.status_code == 200
    assert response.content == b"mp4"
    assert 'filename="songexport.mp4"' in response.headers["content-disposition"]
    assert not work.exists()


if __name__ == "__main__":
    unittest.main()
