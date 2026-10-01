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

## Requirements

Visual Export needs four independent things. Only the first is a feedBack
requirement; the rest are separate and are checked separately.

### feedBack host (required for every export)

- `window.highway.renderFrameAt(time)`, which paints one explicit chart time
  without sampling the playback clock. It arrived in feedBack commit
  `f7c761c` (Sep 16 2026); see get-flashbacks/feedBack issue #102.

The exporter also calls `window.highway.getSongInfo()` and
`window.highway.getSections()`, but both are long-standing feedBack APIs that
predate `f7c761c` by months and are not part of the export's floor.

There is deliberately **no `minHost` version in the manifest**. feedBack's
`VERSION` file has read `0.3.0-alpha.2` since 2026-08-10 — before
`renderFrameAt()` existed — and feedBack publishes no releases, so no version
string identifies a build that can export. Rather than declare a number that is
either too low or does not exist, the plugin checks for the interface itself
and, when it is absent, says which build to update to.

### Split Screen (required only when exporting a split layout)

When Split Screen is active, the export additionally needs its offline-render
bridge: `beginOfflineRender()`, `renderFrameAt(time)`, and
`endOfflineRender()`. The bridge arrived in Split Screen **1.14.8** (commits
`2301dd5` and `87e3622a`); 1.14.7 is the last version without it. All three are
required together, because Split Screen's `renderFrameAt()` refuses to paint
unless `beginOfflineRender()` has already run — so a partial bridge could never
export in the first place. The exporter checks all three up front and reports
the missing ones by name instead of failing mid-render. Without Split Screen
installed, single-highway export is unaffected.

### WebCodecs (browser)

Chromium-family browser with H.264 `VideoEncoder` support. Missing WebCodecs is
reported on its own, separately from any host or Split Screen problem.

### FFmpeg (server)

FFmpeg reachable through `PATH` or the desktop application's bundled
`resources/bin` folder. It muxes the encoded frames with the song audio and is
only needed once rendering succeeds, so its absence surfaces as a muxing
failure rather than a host compatibility error.

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
