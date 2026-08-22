# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistence and FFmpeg muxing for Visual Export."""

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

PLUGIN_ID = "visual-export"
MAX_SETTINGS_BODY_BYTES = 16 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 1024 * 1024 * 1024
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
        return JSONResponse({"ffmpeg": bool(_ffmpeg_cmd()), "format": "mp4"})

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
        if fps not in {24, 30, 60} or not (0 < duration <= 8 * 60 * 60):
            return JSONResponse({"error": "invalid export timing"}, status_code=400)
        work = Path(tempfile.mkdtemp(prefix="feedback-visual-export-"))
        video_path, audio_path, output_path = work / "frames.h264", work / "audio.bin", work / "export.mp4"
        try:
            await _save_upload(video, video_path, MAX_VIDEO_BYTES)
            await _save_upload(audio, audio_path, MAX_AUDIO_BYTES)
            proc = await asyncio.to_thread(
                subprocess.run,
                [ffmpeg, "-y", "-loglevel", "error", "-r", str(fps),
                 "-f", "h264", "-i", str(video_path), "-i", str(audio_path),
                 "-t", f"{duration:.6f}", "-c:v", "copy", "-c:a", "aac",
                 "-b:a", "192k", "-movflags", "+faststart", str(output_path)],
                capture_output=True, text=True,
                timeout=max(120, int(duration * 2)), check=False,
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

        safe_name = "".join(c for c in filename if c.isalnum() or c in "-_").strip("-_")
        background_tasks.add_task(shutil.rmtree, work, ignore_errors=True)
        return FileResponse(output_path, media_type="video/mp4",
                            filename=(safe_name or "feedback-export") + ".mp4",
                            background=background_tasks)

    log.info("%s: routes registered", PLUGIN_ID)
