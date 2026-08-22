# Visual Export

Visual Export renders the currently configured fee[dB]ack player to an MP4
without playing the song in real time. It supports the normal player and active
Splitscreen layouts, including arrangement panels and Splitscreen lyrics.

## Use

1. Load a song and configure the player, visualization, camera, lyrics, and
   Splitscreen panels exactly as wanted.
2. Open the player **Plugins** rail and choose **Export video**.
3. Select resolution, frame rate, and bitrate. Playback controls are omitted.
4. Choose **Render MP4**. The song remains paused while frames are rendered and
   returns to its previous position afterward. The completed MP4 downloads
   automatically.

The export speed depends on visualization cost, output resolution, and the
browser's hardware H.264 encoder. It is not tied to the song's playback speed.

## Requirements

- A Chromium-family browser with WebCodecs H.264 encoding.
- FFmpeg on the fee[dB]ack server (`PATH` or the desktop bundled `resources/bin`).
- A host highway with `renderFrameAt(time)` support.
- For split layouts, a Splitscreen build with its offline-render bridge.
- Browser-routed song audio. Native/JUCE-only output without an HTML audio URL
  cannot currently be muxed.

## Pipeline

The browser advances each highway to an explicit timestamp, composites visible
player canvases and UI overlays into an export canvas, and feeds that canvas to
`VideoEncoder`. H.264 frames and the original song audio are uploaded to the
plugin's bounded mux endpoint; FFmpeg adds AAC audio and returns a fast-start MP4.

The compositor caches the static scene once. Only canvas pixels, changing text,
and Splitscreen lyric spans are refreshed per frame, avoiding a full DOM/layout
scan during long exports. Output with a different aspect ratio is letterboxed
instead of stretching the configured scene.

## Current boundaries

- The exported audio is the HTML player's current source. An audio mix rendered
  exclusively by the native/JUCE engine is not yet available to this plugin.
- DOM overlays are reproduced by the lightweight compositor; unusual third-party
  CSS effects (complex SVG/filter/mask effects) may not be pixel-identical.
- Ambient particles and other effects driven by wall-clock animation are not
  synchronized by the offline renderer and may differ from a live recording.
- Jumping Tab panels do not yet expose deterministic frame control and are not
  included in the supported offline layouts.
