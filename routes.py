# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistence and FFmpeg muxing for Visual Export."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

PLUGIN_ID = "visual_export"
MAX_SETTINGS_BODY_BYTES = 16 * 1024
MAX_SESSION_BODY_BYTES = 16 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 1024 * 1024 * 1024
# Issue #9, phase 3: a session is abandoned if the browser goes away mid-export
# (closed tab, crashed render), so live sessions are swept on creation.
SESSION_TTL_SECONDS = 2 * 60 * 60
MAX_SESSIONS = 8
# Same-host package audio is resolved server-side so the browser never has to
# download and re-upload the song mix. Anything that isn't a plain same-origin
# path is refused here and the browser falls back to uploading it itself.
AUDIO_FETCH_TIMEOUT_SECONDS = 60
_DEFAULTS = {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "bitrate_mbps": 10,
    "include_chrome": False,
}


def _is_valid_setting(name: str, value: object) -> bool:
    if name == "width":
        return type(value) is int and value in {1280, 1920, 2560, 3840}
    if name == "height":
        return type(value) is int and value in {720, 1080, 1440, 2160}
    if name == "fps":
        return type(value) is int and value in {24, 30, 60}
    if name == "bitrate_mbps":
        return type(value) is int and 2 <= value <= 80
    if name == "include_chrome":
        return type(value) is bool
    return False


def _ffmpeg_cmd() -> str | None:
    """Resolve PATH or desktop-bundled FFmpeg without importing core internals."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    exe_dir = Path(sys.executable).resolve().parent
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates = [exe_dir / "resources" / "bin" / name, exe_dir / "bin" / name]
    # Covers source installs and desktop layouts where this plugin sits below
    # resources/feedBack/plugins while FFmpeg sits in resources/bin.
    for parent in Path(__file__).resolve().parents:
        candidates.extend((parent / "bin" / name, parent / "resources" / "bin" / name))
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.insert(0, Path(bundle) / "resources" / "bin" / name)
    return next((str(p) for p in candidates if p.is_file()), None)


async def _save_upload(upload: UploadFile, path: Path, limit: int) -> None:
    total = 0
    with path.open("wb") as target:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("uploaded file is too large")
            target.write(chunk)


def _valid_timing(fps: object, duration: object) -> bool:
    if isinstance(fps, bool) or not isinstance(fps, int) or fps not in {24, 30, 60}:
        return False
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return False
    return 0 < duration <= 8 * 60 * 60


def _safe_filename(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return "".join(c for c in name if c.isalnum() or c in "-_").strip("-_")


def _same_host_path(url: object) -> str | None:
    """Return `url` when it is a same-origin absolute path, else None.

    Only site-relative paths are accepted: a scheme, a protocol-relative
    "//host" prefix, backslashes or any ".." segment could otherwise point the
    server at a different host or outside the song library.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        return None
    if not url.startswith("/") or url.startswith("//"):
        return None
    if "\\" in url or any(urllib.parse.urlsplit(part).scheme or part == ".." for part in url.split("/")):
        return None
    return url


def _run_ffmpeg(ffmpeg: str, video_path: Path, audio_path: Path, output_path: Path, fps: int, duration: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-r", str(fps),
         "-f", "h264", "-i", str(video_path), "-i", str(audio_path),
         "-t", f"{duration:.6f}", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "192k", "-movflags", "+faststart", str(output_path)],
        capture_output=True, text=True,
        timeout=max(120, int(duration * 2)), check=False,
    )


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so a validated path cannot be bounced off-host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, f"refusing to follow {code} redirect", headers, fp)


#: opener used only for the same-host audio fetch below. It carries no cookie
#: or auth jar, so a 302 cannot be used to borrow the host's credentials.
_AUDIO_OPENER = urllib.request.build_opener(_NoRedirects)


def _expected_origin() -> str | None:
    """This host's own origin, from configuration only.

    ``request.base_url`` is built from the client-supplied ``Host`` header, so
    a raw client could otherwise aim the fetch at a host of its choosing. Only
    a host configured by the operator is trusted here; when there is none the
    audio is left for the browser to upload rather than fetched speculatively.
    """
    configured = os.environ.get("FEEDBACK_PUBLIC_ORIGIN", "").strip()
    if not configured:
        return None
    parts = urllib.parse.urlsplit(configured if "//" in configured else "http://" + configured)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.path.rstrip("/"):
        return None
    return f"{parts.scheme}://{parts.netloc}"


def setup(app: FastAPI, context: dict) -> None:
    config_dir = Path(context["config_dir"])
    log = context.get("log") or logging.getLogger(f"feedBack.plugin.{PLUGIN_ID}")
    config_file = config_dir / f"{PLUGIN_ID}.json"

    def _read() -> dict:
        settings = dict(_DEFAULTS)
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                settings.update({k: v for k, v in data.items() if _is_valid_setting(k, v)})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("%s: unreadable config, using defaults: %s", PLUGIN_ID, exc)
        return settings

    @app.get(f"/api/plugins/{PLUGIN_ID}/settings")
    def get_settings() -> JSONResponse:
        return JSONResponse(_read())

    @app.post(f"/api/plugins/{PLUGIN_ID}/settings")
    async def set_settings(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > MAX_SETTINGS_BODY_BYTES:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        try:
            incoming = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(incoming, dict) or incoming.keys() - _DEFAULTS.keys():
            return JSONResponse({"error": "unknown settings"}, status_code=400)
        if not all(_is_valid_setting(k, v) for k, v in incoming.items()):
            return JSONResponse({"error": "invalid setting values"}, status_code=400)
        merged = {**_read(), **incoming}
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                config_file.write_text, json.dumps(merged, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.error("%s: failed to save settings: %s", PLUGIN_ID, exc)
            return JSONResponse({"error": "failed to save settings"}, status_code=500)
        return JSONResponse(merged)

    @app.get(f"/api/plugins/{PLUGIN_ID}/capabilities")
    def capabilities() -> JSONResponse:
        return JSONResponse({"ffmpeg": bool(_ffmpeg_cmd()), "format": "mp4", "sessions": True})

    # Issue #9, phase 3: streaming export sessions. The browser posts encoded
    # H.264 chunks as the encoder produces them, so the complete elementary
    # stream is never held in browser memory, and the server resolves the
    # song mix from its own host instead of receiving an uploaded copy. The
    # single-request /mux route below stays available and unchanged as the
    # fallback for hosts or sources that cannot use a session.
    sessions: dict[str, dict] = {}

    def _drop_session(session_id: str) -> None:
        state = sessions.pop(session_id, None)
        if state:
            shutil.rmtree(state["work"], ignore_errors=True)

    def _sweep_sessions() -> None:
        now = time.monotonic()
        for session_id, state in list(sessions.items()):
            if now - state["touched"] > SESSION_TTL_SECONDS:
                log.info("%s: expiring abandoned export session %s", PLUGIN_ID, session_id)
                _drop_session(session_id)
        while len(sessions) >= MAX_SESSIONS:
            oldest = min(sessions, key=lambda key: sessions[key]["touched"])
            _drop_session(oldest)

    async def _json_body(request: Request) -> dict | None:
        raw = await request.body()
        if len(raw) > MAX_SESSION_BODY_BYTES:
            return None
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _fetch_host_audio(origin: str, relative: str, target: Path) -> None:
        """Download `relative` from `origin` without ever leaving that origin.

        Redirects are refused rather than followed: validating the *path* only
        constrains the string, never where the response came from, so a 302
        would otherwise let this server fetch an arbitrary host. A host that
        legitimately redirects song mixes elsewhere is better served by the
        browser uploading the audio (the `POST /audio` route).
        """

        def _download() -> None:
            url = origin + relative
            request = urllib.request.Request(url, headers={"User-Agent": "feedback-visual-export"})
            total = 0
            with _AUDIO_OPENER.open(request, timeout=AUDIO_FETCH_TIMEOUT_SECONDS) as response, target.open("wb") as out:
                # Defence in depth behind the refused redirects: confirm the
                # response really came from the origin we asked.
                final = urllib.parse.urlsplit(response.geturl())
                if f"{final.scheme}://{final.netloc}" != origin:
                    raise ValueError("song audio came from an unexpected host")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise ValueError("song audio is too large")
                    out.write(chunk)

        await asyncio.to_thread(_download)

    @app.post(f"/api/plugins/{PLUGIN_ID}/sessions")
    async def create_session(request: Request) -> JSONResponse:
        ffmpeg = _ffmpeg_cmd()
        if not ffmpeg:
            return JSONResponse({"error": "FFmpeg is not installed"}, status_code=503)
        data = await _json_body(request)
        if data is None:
            return JSONResponse({"error": "invalid session request"}, status_code=400)
        if not _valid_timing(data.get("fps"), data.get("duration")):
            return JSONResponse({"error": "invalid export timing"}, status_code=400)
        audio_url = data.get("audio_url")
        if audio_url is not None and _same_host_path(audio_url) is None:
            return JSONResponse({"error": "audio_url must be a same-host path"}, status_code=400)
        _sweep_sessions()
        session_id = uuid.uuid4().hex
        sessions[session_id] = {
            "work": Path(tempfile.mkdtemp(prefix="feedback-visual-export-")),
            "touched": time.monotonic(),
            "origin": _expected_origin(),
            "audio_url": audio_url,
            # Cumulative across every append, so the session as a whole is
            # bounded by MAX_VIDEO_BYTES the way the single-request upload was.
            "video_bytes": 0,
            # Serializes concurrent chunk appends so two overlapping requests
            # can never interleave writes into the same elementary stream.
            "append_lock": asyncio.Lock(),
        }
        return JSONResponse({"session": session_id,
                             # Only true when this session can actually resolve a
                             # mix: both a configured origin and a path to use.
                             "audio_fetch": bool(sessions[session_id]["origin"] and audio_url)})

    @app.post(f"/api/plugins/{PLUGIN_ID}/sessions/{{session_id}}/video")
    async def append_session_video(session_id: str, request: Request) -> JSONResponse:
        state = sessions.get(session_id)
        if state is None:
            return JSONResponse({"error": "unknown or expired export session"}, status_code=404)
        state["touched"] = time.monotonic()
        video_path = state["work"] / "frames.h264"
        total = 0
        # Snapshot the counter before the request: it is the file's size, so it
        # must be restored to exactly this value on failure rather than adjusted
        # by whatever this request happened to receive.
        start = state["video_bytes"]
        try:
            async with state["append_lock"]:
                try:
                    with video_path.open("ab") as target:
                        async for chunk in request.stream():
                            if not chunk:
                                continue
                            total += len(chunk)
                            # Running total, not this request's total: streaming
                            # must not turn the old whole-upload cap into a
                            # per-request one.
                            if start + total > MAX_VIDEO_BYTES:
                                raise ValueError("encoded video is too large")
                            target.write(chunk)
                    state["video_bytes"] = start + total
                except (OSError, ValueError):
                    # Roll back inside the lock. Doing it after release would
                    # let a concurrent append land between the write and the
                    # truncate, and this rollback would then discard an accepted
                    # chunk and un-bind the cap.
                    state["video_bytes"] = start
                    if total:
                        try:
                            os.truncate(video_path, start)
                        except OSError:
                            # The file is unusable anyway; drop it rather than
                            # leave a partial stream for a later mux to pick up.
                            state["video_bytes"] = 0
                            video_path.unlink(missing_ok=True)
                    raise
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"bytes": total})

    @app.post(f"/api/plugins/{PLUGIN_ID}/sessions/{{session_id}}/audio")
    async def upload_session_audio(session_id: str, request: Request) -> JSONResponse:
        state = sessions.get(session_id)
        if state is None:
            return JSONResponse({"error": "unknown or expired export session"}, status_code=404)
        state["touched"] = time.monotonic()
        audio_path = state["work"] / "audio.bin"
        if audio_path.exists():
            return JSONResponse({"error": "audio was already uploaded for this session"}, status_code=409)
        total = 0
        try:
            with audio_path.open("wb") as target:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise ValueError("song audio is too large")
                    target.write(chunk)
        except (OSError, ValueError) as exc:
            audio_path.unlink(missing_ok=True)
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"bytes": total})

    @app.post(f"/api/plugins/{PLUGIN_ID}/sessions/{{session_id}}/mux")
    async def mux_session(background_tasks: BackgroundTasks, session_id: str, request: Request):
        ffmpeg = _ffmpeg_cmd()
        if not ffmpeg:
            return JSONResponse({"error": "FFmpeg is not installed"}, status_code=503)
        state = sessions.get(session_id)
        if state is None:
            return JSONResponse({"error": "unknown or expired export session"}, status_code=404)
        data = await _json_body(request)
        if data is None:
            return JSONResponse({"error": "invalid session request"}, status_code=400)
        if not _valid_timing(data.get("fps"), data.get("duration")):
            return JSONResponse({"error": "invalid export timing"}, status_code=400)
        work = state["work"]
        video_path, audio_path, output_path = work / "frames.h264", work / "audio.bin", work / "export.mp4"
        if not video_path.is_file() or video_path.stat().st_size == 0:
            return JSONResponse({"error": "no encoded video was uploaded for this session"}, status_code=400)

        # Resolve the audio BEFORE consuming the session. A mix the server
        # cannot fetch (no configured origin, a 404, an auth wall, or any
        # redirect now refused) leaves the session intact so the browser can
        # upload the audio and ask again, instead of losing a full render at
        # the last step.
        # `dict.get(key, default)` only falls back when the key is ABSENT, and
        # the browser always sends `audio_url` -- as null when it has already
        # uploaded the mix or wants the session's stored path. So an explicit
        # null means "no opinion", not "no audio".
        audio_url = data.get("audio_url") or state["audio_url"]
        origin = state["origin"]
        if not audio_path.is_file():
            relative = _same_host_path(audio_url) if audio_url is not None else None
            if audio_url is not None and origin is not None and relative is None:
                return JSONResponse({"error": "audio_url must be a same-host path"}, status_code=400)
            if relative is None or origin is None:
                return JSONResponse({"error": "no song audio was recorded for this session",
                                     "audio_required": True}, status_code=409)
            try:
                await _fetch_host_audio(origin, relative, audio_path)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                audio_path.unlink(missing_ok=True)
                log.warning("%s: could not fetch song audio, asking the browser to upload it: %s", PLUGIN_ID, exc)
                return JSONResponse({"error": str(exc), "audio_required": True}, status_code=409)

        # Past this point the session is consumed: every exit path below removes
        # its temporary directory, and a mux must not be repeatable.
        sessions.pop(session_id, None)
        try:
            proc = await asyncio.to_thread(
                _run_ffmpeg, ffmpeg, video_path, audio_path, output_path, data["fps"], float(data["duration"])
            )
            if proc.returncode != 0 or not output_path.is_file():
                detail = (proc.stderr or "FFmpeg failed").strip()[-2000:]
                log.error("%s: mux failed: %s", PLUGIN_ID, detail)
                shutil.rmtree(work, ignore_errors=True)
                return JSONResponse({"error": detail}, status_code=500)
        except (OSError, ValueError, urllib.error.URLError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(work, ignore_errors=True)
            return JSONResponse({"error": str(exc)}, status_code=400)

        safe_name = _safe_filename(data.get("filename")) or "feedback-export"
        background_tasks.add_task(shutil.rmtree, work, ignore_errors=True)
        return FileResponse(output_path, media_type="video/mp4", filename=safe_name + ".mp4",
                            background=background_tasks)

    @app.delete(f"/api/plugins/{PLUGIN_ID}/sessions/{{session_id}}")
    async def cancel_session(session_id: str) -> JSONResponse:
        if session_id not in sessions:
            return JSONResponse({"error": "unknown or expired export session"}, status_code=404)
        _drop_session(session_id)
        return JSONResponse({"status": "cancelled"})

    @app.post(f"/api/plugins/{PLUGIN_ID}/mux")
    async def mux_export(
        background_tasks: BackgroundTasks,
        video: UploadFile = File(...),
        audio: UploadFile = File(...),
        fps: int = Form(...),
        duration: float = Form(...),
        filename: str = Form("feedback-export"),
    ):
        ffmpeg = _ffmpeg_cmd()
        if not ffmpeg:
            return JSONResponse({"error": "FFmpeg is not installed"}, status_code=503)
        if not _valid_timing(fps, duration):
            return JSONResponse({"error": "invalid export timing"}, status_code=400)
        work = Path(tempfile.mkdtemp(prefix="feedback-visual-export-"))
        video_path, audio_path, output_path = work / "frames.h264", work / "audio.bin", work / "export.mp4"
        try:
            await _save_upload(video, video_path, MAX_VIDEO_BYTES)
            await _save_upload(audio, audio_path, MAX_AUDIO_BYTES)
            proc = await asyncio.to_thread(
                _run_ffmpeg, ffmpeg, video_path, audio_path, output_path, fps, float(duration)
            )
            if proc.returncode != 0 or not output_path.is_file():
                detail = (proc.stderr or "FFmpeg failed").strip()[-2000:]
                log.error("%s: mux failed: %s", PLUGIN_ID, detail)
                shutil.rmtree(work, ignore_errors=True)
                return JSONResponse({"error": detail}, status_code=500)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(work, ignore_errors=True)
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            await video.close()
            await audio.close()

        safe_name = _safe_filename(filename) or "feedback-export"
        background_tasks.add_task(shutil.rmtree, work, ignore_errors=True)
        return FileResponse(output_path, media_type="video/mp4",
                            filename=safe_name + ".mp4",
                            background=background_tasks)

    log.info("%s: routes registered", PLUGIN_ID)
