import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest
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
        self.assertIn((f"{base}/sessions", "POST"), registered)
        self.assertIn((f"{base}/sessions/{{session_id}}/video", "POST"), registered)
        self.assertIn((f"{base}/sessions/{{session_id}}/audio", "POST"), registered)
        self.assertIn((f"{base}/sessions/{{session_id}}/mux", "POST"), registered)
        self.assertIn((f"{base}/sessions/{{session_id}}", "DELETE"), registered)


class SameHostPathTest(unittest.TestCase):
    def test_accepts_site_relative_paths(self):
        self.assertEqual(routes._same_host_path("/songs/pack/full_mix.wav"), "/songs/pack/full_mix.wav")

    def test_rejects_anything_that_could_leave_the_host(self):
        for url in (
            "https://elsewhere.example/song.wav",
            "//elsewhere.example/song.wav",
            "/../etc/passwd",
            "songs/pack/full_mix.wav",
            "/songs\\pack.wav",
            "",
            None,
        ):
            self.assertIsNone(routes._same_host_path(url), url)


class ValidTimingTest(unittest.TestCase):
    def test_accepts_supported_frame_rates(self):
        self.assertTrue(routes._valid_timing(30, 180.0))
        self.assertTrue(routes._valid_timing(24, 1))

    def test_rejects_bad_values(self):
        self.assertFalse(routes._valid_timing(25, 180.0))
        self.assertFalse(routes._valid_timing(True, 180.0))
        self.assertFalse(routes._valid_timing(30, 0))
        self.assertFalse(routes._valid_timing(30, True))
        self.assertFalse(routes._valid_timing(30, "180"))
        self.assertFalse(routes._valid_timing(30, 9 * 60 * 60))


def _client(tmp_path):
    app = FastAPI()
    routes.setup(app, {"config_dir": tmp_path})
    return TestClient(app)


def _isolated_work(tmp_path, monkeypatch):
    """Point session temp dirs at tmp_path, one fresh dir per session."""
    counter = [0]

    def mkdtemp(**kwargs):
        counter[0] += 1
        path = tmp_path / f"work{counter[0]}"
        path.mkdir()
        return str(path)

    monkeypatch.setattr(routes.tempfile, "mkdtemp", mkdtemp)


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



def test_mux_reports_missing_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: None)

    response = _export(_client(tmp_path))
    assert response.status_code == 503
    assert response.json() == {"error": "FFmpeg is not installed"}


def test_mux_reports_ffmpeg_failure_and_removes_work_dir(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="mux failure"),
    )

    response = _export(_client(tmp_path))
    assert response.status_code == 500
    assert response.json() == {"error": "mux failure"}
    assert not work.exists()


def _session(client, **payload):
    body = {"fps": 30, "duration": 1.0, "filename": "song", **payload}
    return client.post("/api/plugins/visual_export/sessions", json=body)


def _stub_audio(monkeypatch, audio=b"audio", origin="http://feedback.test"):
    """Stand in for the host serving the song mix back over its own origin."""
    monkeypatch.setenv("FEEDBACK_PUBLIC_ORIGIN", origin)

    class _Response(io.BytesIO):
        def __init__(self, request):
            super().__init__(audio)
            self._url = str(request.full_url)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

        def geturl(self):
            return self._url

    class _Opener:
        def open(self, request, timeout=None):
            assert str(request.full_url).endswith("/songs/pack/full_mix.wav")
            assert str(request.full_url).startswith(origin + "/")
            assert timeout is not None
            return _Response(request)

    monkeypatch.setattr(routes, "_AUDIO_OPENER", _Opener())


def test_audio_opener_refuses_redirects(tmp_path, monkeypatch):
    """A validated path must not be usable to fetch some other host."""
    import http.server
    import threading
    import urllib.error

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://elsewhere.invalid/secret")
            self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            routes._AUDIO_OPENER.open(
                f"http://127.0.0.1:{server.server_address[1]}/songs/full_mix.wav", timeout=5
            )
        assert caught.value.code == 302
    finally:
        server.shutdown()


def _stub_ffmpeg(monkeypatch, work, video=b"video", audio=b"audio"):
    def fake_ffmpeg(cmd, **kwargs):
        assert (work / "frames.h264").read_bytes() == video
        assert (work / "audio.bin").read_bytes() == audio
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_ffmpeg)


def test_session_streams_chunks_then_muxes_server_resolved_audio(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_audio(monkeypatch)
    _stub_ffmpeg(monkeypatch, work, video=b"chunkone" + b"chunktwo")
    client = _client(tmp_path)

    created = _session(client, audio_url="/songs/pack/full_mix.wav")
    assert created.status_code == 200
    session = created.json()["session"]

    for chunk in (b"chunkone", b"chunktwo"):
        response = client.post(
            f"/api/plugins/visual_export/sessions/{session}/video",
            content=chunk,
            headers={"Content-Type": "video/h264"},
        )
        assert response.status_code == 200
    assert (work / "frames.h264").read_bytes() == b"chunkonechunktwo"

    muxed = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux",
        json={"fps": 30, "duration": 1.0, "filename": "song / export"},
    )
    assert muxed.status_code == 200
    assert muxed.content == b"mp4"
    assert 'filename="songexport.mp4"' in muxed.headers["content-disposition"]
    assert not work.exists()

    # A session is single-use: a second mux must not reuse the stale work dir.
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux",
        json={"fps": 30, "duration": 1.0, "filename": "song"},
    ).status_code == 404


def test_session_rejects_invalid_timing_before_creating_work_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")

    def unexpected_work_dir(**kwargs):
        raise AssertionError("invalid timing must not create a work directory")

    monkeypatch.setattr(routes.tempfile, "mkdtemp", unexpected_work_dir)
    response = _client(tmp_path).post(
        "/api/plugins/visual_export/sessions", json={"fps": 25, "duration": 1.0}
    )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid export timing"}


def test_session_rejects_audio_url_that_is_not_same_host(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")

    def unexpected_work_dir(**kwargs):
        raise AssertionError("an off-host audio URL must not create a work directory")

    monkeypatch.setattr(routes.tempfile, "mkdtemp", unexpected_work_dir)
    response = _session(_client(tmp_path), audio_url="https://elsewhere.example/song.wav")
    assert response.status_code == 400
    assert response.json() == {"error": "audio_url must be a same-host path"}


def test_session_video_upload_enforces_size_limit_across_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes, "MAX_VIDEO_BYTES", 5)
    _isolated_work(tmp_path, monkeypatch)
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    video = tmp_path / "work1" / "frames.h264"

    # Each append is under the limit on its own; the session total must still be
    # capped, or streaming silently removes the bound the old upload had.
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/video", content=b"three"
    ).status_code == 200
    assert video.read_bytes() == b"three"
    response = client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"four")
    assert response.status_code == 400
    assert "too large" in response.json()["error"]
    # Only the rejected request's bytes are rolled back, so the chunks already
    # accepted survive...
    assert video.read_bytes() == b"three"
    # ...and the counter is still the file's size, so a further append that
    # would fit is refused too. A refund smaller than the rejected body (or a
    # counter driven negative) would let this through.
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/video", content=b"two"
    ).status_code == 400
    assert video.read_bytes() == b"three"

    # An oversized first append truncates to nothing rather than leaving a tail.
    second = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    assert client.post(
        f"/api/plugins/visual_export/sessions/{second}/video", content=b"0123456789"
    ).status_code == 400
    assert (tmp_path / "work2" / "frames.h264").read_bytes() == b""


def test_rejected_append_keeps_earlier_chunks_and_the_cap(tmp_path, monkeypatch):
    """A rejected append must not delete the chunks already streamed."""
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes, "MAX_VIDEO_BYTES", 5)
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_audio(monkeypatch)
    _stub_ffmpeg(monkeypatch, work, video=b"12345")
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    url = f"/api/plugins/visual_export/sessions/{session}/video"

    assert client.post(url, content=b"12345").status_code == 200
    assert client.post(url, content=b"0123456789").status_code == 400
    assert (work / "frames.h264").read_bytes() == b"12345"
    assert client.post(url, content=b"x").status_code == 400
    # The 5 accepted bytes are still muxable, which is the point of truncating
    # rather than unlinking.
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    ).status_code == 200


def test_audio_fetch_requires_a_configured_origin(tmp_path, monkeypatch):
    """A client-supplied Host header must not be able to aim the fetch.

    _expected_origin() is the only source of the origin the audio fetch uses;
    without one configured the server asks the browser for the mix instead.
    """
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    monkeypatch.delenv("FEEDBACK_PUBLIC_ORIGIN", raising=False)
    _stub_ffmpeg(monkeypatch, work, video=b"video", audio=b"uploaded mix")
    client = _client(tmp_path)
    created = _session(client, audio_url="/songs/pack/full_mix.wav").json()
    session = created["session"]
    # The browser is told up front that the server will not resolve the mix, so
    # it uploads the audio before rendering instead of failing at mux time.
    assert created["audio_fetch"] is False
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")

    # Even with an attacker-chosen Host, nothing is fetched server-side.
    first = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux",
        json={"fps": 30, "duration": 1.0, "audio_url": "/songs/pack/full_mix.wav"},
        headers={"Host": "attacker.example"},
    )
    assert first.status_code == 409
    assert first.json()["audio_required"] is True

    # The session survives so the browser can upload and ask again.
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/audio", content=b"uploaded mix"
    ).status_code == 200
    second = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    )
    assert second.status_code == 200
    assert second.content == b"mp4"
    assert not work.exists()


def test_session_advertises_audio_fetch_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    _isolated_work(tmp_path, monkeypatch)
    monkeypatch.delenv("FEEDBACK_PUBLIC_ORIGIN", raising=False)
    assert _session(_client(tmp_path)).json()["audio_fetch"] is False
    monkeypatch.setenv("FEEDBACK_PUBLIC_ORIGIN", "http://feedback.test")
    assert _session(_client(tmp_path)).json()["audio_fetch"] is True


def test_expected_origin_ignores_unusable_configuration(monkeypatch):
    for value in ("", "   ", "https://host/app", "ftp://host", "not a url/../x"):
        monkeypatch.setenv("FEEDBACK_PUBLIC_ORIGIN", value)
        assert routes._expected_origin() is None, value
    monkeypatch.setenv("FEEDBACK_PUBLIC_ORIGIN", "http://127.0.0.1:5173")
    assert routes._expected_origin() == "http://127.0.0.1:5173"
    monkeypatch.setenv("FEEDBACK_PUBLIC_ORIGIN", "https://feedback.example")
    assert routes._expected_origin() == "https://feedback.example"


def test_session_mux_reports_audio_fetch_failure_and_keeps_the_session(tmp_path, monkeypatch):
    """A fetch that fails mid-mux must not discard the rendered video."""
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    monkeypatch.setenv("FEEDBACK_PUBLIC_ORIGIN", "http://feedback.test")

    class _Failing:
        def open(self, request, timeout=None):
            raise OSError("connection refused")

    monkeypatch.setattr(routes, "_AUDIO_OPENER", _Failing())
    _stub_ffmpeg(monkeypatch, work, audio=b"audio")
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")

    response = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    )
    assert response.status_code == 409
    assert "connection refused" in response.json()["error"]
    assert response.json()["audio_required"] is True
    # The half of a fetch that failed left nothing behind, and the session is
    # intact so the browser can upload the mix and ask again.
    assert not (work / "audio.bin").exists()
    assert (work / "frames.h264").exists()
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/audio", content=b"audio"
    ).status_code == 200
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    ).status_code == 200
    assert not work.exists()


def test_session_mux_reports_ffmpeg_failure_and_removes_work_dir(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_audio(monkeypatch)
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="session mux failure"),
    )
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")

    response = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    )
    assert response.status_code == 500
    assert response.json() == {"error": "session mux failure"}
    assert not work.exists()


def test_session_audio_upload_muxes_without_server_side_fetch(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_ffmpeg(monkeypatch, work, video=b"video", audio=b"uploaded mix")
    client = _client(tmp_path)

    session = _session(client).json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")
    uploaded = client.post(
        f"/api/plugins/visual_export/sessions/{session}/audio", content=b"uploaded mix"
    )
    assert uploaded.status_code == 200
    # A second upload would silently replace the mix that was already muxed.
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/audio", content=b"other"
    ).status_code == 409

    muxed = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    )
    assert muxed.status_code == 200
    assert muxed.content == b"mp4"
    assert not work.exists()


def test_session_mux_requires_audio(tmp_path, monkeypatch):
    """A mix the server cannot resolve keeps the session alive for an upload."""
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_ffmpeg(monkeypatch, work, video=b"video", audio=b"late mix")
    client = _client(tmp_path)
    session = _session(client).json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")

    response = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    )
    assert response.status_code == 409
    assert response.json()["audio_required"] is True
    # The rendered video survives the refusal rather than being discarded.
    assert (work / "frames.h264").read_bytes() == b"video"
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/audio", content=b"late mix"
    ).status_code == 200
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    ).status_code == 200
    assert not work.exists()


def test_session_mux_rejects_a_second_mux_after_success(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_audio(monkeypatch)
    _stub_ffmpeg(monkeypatch, work)
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")
    body = {"fps": 30, "duration": 1.0}
    assert client.post(f"/api/plugins/visual_export/sessions/{session}/mux", json=body).status_code == 200
    assert client.post(f"/api/plugins/visual_export/sessions/{session}/mux", json=body).status_code == 404


def test_session_mux_requires_uploaded_video(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    _stub_audio(monkeypatch)
    _stub_ffmpeg(monkeypatch, work)
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]

    response = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    )
    assert response.status_code == 400
    assert "no encoded video" in response.json()["error"]
    # Nothing was rendered yet, so the session is kept and the browser can
    # stream the video and ask again.
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 30, "duration": 1.0}
    ).status_code == 200
    assert not work.exists()


def test_session_mux_rejects_invalid_timing_and_keeps_the_session(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")

    response = client.post(
        f"/api/plugins/visual_export/sessions/{session}/mux", json={"fps": 25, "duration": 1.0}
    )
    assert response.status_code == 400
    # A rejected request must not destroy the upload the browser already sent.
    assert (work / "frames.h264").read_bytes() == b"video"
    assert client.delete(f"/api/plugins/visual_export/sessions/{session}").status_code == 200


def test_cancel_session_removes_work_dir_and_unknown_session_is_404(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes.tempfile, "mkdtemp", lambda **kwargs: str(work.mkdir() or work))
    client = _client(tmp_path)
    session = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{session}/video", content=b"video")
    assert work.exists()

    assert client.delete(f"/api/plugins/visual_export/sessions/{session}").status_code == 200
    assert not work.exists()
    assert client.delete(f"/api/plugins/visual_export/sessions/{session}").status_code == 404
    assert client.post(
        f"/api/plugins/visual_export/sessions/{session}/video", content=b"video"
    ).status_code == 404


def test_abandoned_sessions_are_expired(tmp_path, monkeypatch):
    work = tmp_path / "works"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes, "SESSION_TTL_SECONDS", 0)
    counter = iter(range(100))

    def fake_mkdtemp(**kwargs):
        path = work / f"work{next(counter)}"
        path.mkdir(parents=True)
        return str(path)

    monkeypatch.setattr(routes.tempfile, "mkdtemp", fake_mkdtemp)
    client = _client(tmp_path)
    first = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    client.post(f"/api/plugins/visual_export/sessions/{first}/video", content=b"video")

    second = _session(client, audio_url="/songs/pack/full_mix.wav").json()["session"]
    assert second != first
    assert not (work / "work0").exists()
    assert (work / "work1").exists()


def test_session_limit_drops_the_oldest_session(tmp_path, monkeypatch):
    work = tmp_path / "works"
    monkeypatch.setattr(routes, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(routes, "MAX_SESSIONS", 2)
    counter = iter(range(100))

    def fake_mkdtemp(**kwargs):
        path = work / f"work{next(counter)}"
        path.mkdir(parents=True)
        return str(path)

    monkeypatch.setattr(routes.tempfile, "mkdtemp", fake_mkdtemp)
    client = _client(tmp_path)
    sessions = [_session(client).json()["session"] for _ in range(3)]
    assert len(set(sessions)) == 3
    assert not (work / "work0").exists()
    assert (work / "work1").exists()
    assert (work / "work2").exists()
