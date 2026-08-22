# Visual Export for fee[dB]ack

Visual Export creates an MP4 of the song currently configured in the fee[dB]ack
player without having to play and screen-record it in real time. It exports the
normal player or the active Splitscreen arrangement, so a separate video can be
made for each guitar, bass, keys, and lyrics layout.

## Export a video

1. Load a song and arrange the player exactly as it should appear: highway,
   camera, overlays, lyrics, and (if used) Splitscreen panels.
2. In the player **Plugins** rail, select **Export video**.
3. Choose the output resolution, frame rate, and bitrate, then select
   **Render MP4**.

The player is paused while the exporter renders timestamps directly and its
previous position is restored afterwards. The generated MP4 downloads when
FFmpeg has finished muxing its video and audio.

Player controls are deliberately excluded from the export. Ambient particles
and other wall-clock visual effects are not synchronized as part of the
deterministic render pass.

## How it works

The browser asks each highway to draw an explicit song time, composites the
visible player canvases and supported overlays, and encodes H.264 frames using
WebCodecs. The plugin server uses FFmpeg to mux those frames with the original
browser-routed song audio as AAC in a fast-start MP4.

The browser caches the static portion of the scene once per export; per-frame
work is limited to highway canvases and dynamic text/lyrics. Export speed is
therefore independent of the song's playback duration, but still depends on
visualization cost, resolution, and hardware encoding performance.

## Host integration

The exporter relies on the following host interfaces:

- A highway renderer with `renderFrameAt(time)` and `getCanvas()`.
- When exporting a split layout, Splitscreen's `beginOfflineRender()`,
  `renderFrameAt(time)`, and `endOfflineRender()` bridge.
- Chromium-family WebCodecs H.264 support and FFmpeg available to the server
  through `PATH` or the desktop application's bundled `resources/bin` folder.

## Limits

- Audio must be available through the browser's HTML audio source. A mix that
  exists only in the native/JUCE engine cannot yet be exported.
- Supported overlays are composited in the browser; advanced third-party
  CSS/SVG/filter effects may not reproduce pixel-for-pixel.
- Jumping Tab panes do not provide deterministic frame rendering and are not
  supported in offline split exports.

See [the implementation README](visual-export/README.md) for the runtime
pipeline and development notes.
