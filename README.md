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
WebCodecs. Hardware encoding is requested first and falls back automatically
when the browser cannot provide it. The plugin server uses FFmpeg to mux those
frames with the original song audio as AAC in a fast-start MP4.

The browser caches the static portion of the scene once per export: immutable
backgrounds, borders, labels, and images are precomposed into offscreen layers,
so per-frame work is limited to highway canvases and dynamic text/lyrics.
Encoded frames are streamed to the server as they are produced. Export speed is
therefore independent of the song's playback duration, but still depends on
visualization cost, resolution, and hardware encoding performance.

While rendering, the progress line reports elapsed time and how far the render
is from real time, and the finished export reports its render time and the
encoder that produced it.

### Optional: let the server fetch the song audio

Setting `FEEDBACK_PUBLIC_ORIGIN` to this host's own origin (for example
`http://127.0.0.1:5173`) lets the plugin server fetch the song mix directly, so
the browser skips downloading and re-uploading it. The server reports back
whether it can do that, so the browser only skips the upload when it can — with
the variable unset (the default), the audio is uploaded once to the export
session as before, and nothing changes.

If the server is configured but the fetch fails (a 404, an auth wall, or a
redirect, which it refuses), the export is not thrown away: the session is kept
alive, the browser uploads the mix, and the mux is retried.

The server only ever contacts that configured origin, and it refuses redirects,
so a song URL cannot redirect the fetch to another host.

## Host integration

The exporter relies on the following host interfaces:

- A highway renderer with `renderFrameAt(time)` and `getCanvas()`.
- When exporting a split layout, Splitscreen's `beginOfflineRender()`,
  `renderFrameAt(time)`, and `endOfflineRender()` bridge.
- Chromium-family WebCodecs H.264 support and FFmpeg available to the server
  through `PATH` or the desktop application's bundled `resources/bin` folder.

## Limits

- The exporter prefers the song package's complete `full_mix_url` when available, even
  if the player is currently using an individual stem or native/JUCE routing.
  Songs without a complete mix fall back to the browser audio source, which is
  uploaded once to the export session unless `FEEDBACK_PUBLIC_ORIGIN` lets the
  server fetch it directly.
- Supported overlays are composited in the browser; advanced third-party
  CSS/SVG/filter effects may not reproduce pixel-for-pixel.
- Jumping Tab panes do not provide deterministic frame rendering and are not
  supported in offline split exports.

The runtime implementation is in `screen.js`, `routes.py`, and `settings.html`.
